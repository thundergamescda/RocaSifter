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

-- Version 4: optional staff MFA. No account is enrolled or inferred by migration.
ALTER TABLE ss_sessions ADD COLUMN IF NOT EXISTS mfa_version BIGINT;
ALTER TABLE ss_sessions ADD COLUMN IF NOT EXISTS mfa_authenticated_at TIMESTAMPTZ;
CREATE TABLE IF NOT EXISTS ss_staff_mfa (
    tenant_id UUID NOT NULL, staff_id UUID NOT NULL,
    enabled BOOLEAN NOT NULL DEFAULT FALSE, version BIGINT NOT NULL DEFAULT 0 CHECK(version>=0),
    encrypted_seed TEXT, last_totp_step BIGINT NOT NULL DEFAULT -1 CHECK(last_totp_step>=-1),
    pending_seed TEXT, pending_token_hash TEXT, pending_session_hash TEXT, pending_password_fingerprint TEXT, pending_expires_at TIMESTAMPTZ,
    pending_attempts INTEGER NOT NULL DEFAULT 0 CHECK(pending_attempts BETWEEN 0 AND 5),
    enabled_at TIMESTAMPTZ, created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY(tenant_id,staff_id), FOREIGN KEY(tenant_id,staff_id) REFERENCES ss_staff(tenant_id,id),
    CHECK(enabled=(encrypted_seed IS NOT NULL)),
    CHECK((pending_seed IS NULL AND pending_token_hash IS NULL AND pending_session_hash IS NULL AND pending_password_fingerprint IS NULL AND pending_expires_at IS NULL)
       OR (pending_seed IS NOT NULL AND pending_token_hash IS NOT NULL AND pending_session_hash IS NOT NULL AND pending_password_fingerprint IS NOT NULL AND pending_expires_at IS NOT NULL))
);
CREATE TABLE IF NOT EXISTS ss_mfa_recovery_codes (
    tenant_id UUID NOT NULL, staff_id UUID NOT NULL, mfa_version BIGINT NOT NULL,
    code_hash TEXT NOT NULL CHECK(length(code_hash)=64), used_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(), PRIMARY KEY(tenant_id,staff_id,code_hash),
    FOREIGN KEY(tenant_id,staff_id) REFERENCES ss_staff_mfa(tenant_id,staff_id)
);
CREATE TABLE IF NOT EXISTS ss_mfa_challenges (
    token_hash TEXT PRIMARY KEY CHECK(length(token_hash)=64), tenant_id UUID NOT NULL, staff_id UUID NOT NULL,
    mfa_version BIGINT NOT NULL, password_fingerprint TEXT NOT NULL CHECK(length(password_fingerprint)=64),
    client_hash TEXT NOT NULL CHECK(length(client_hash)=64), expires_at TIMESTAMPTZ NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts BETWEEN 0 AND 5), used_at TIMESTAMPTZ, cancelled_at TIMESTAMPTZ,
    issued_session_hash TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY(tenant_id,staff_id) REFERENCES ss_staff_mfa(tenant_id,staff_id)
);
CREATE INDEX IF NOT EXISTS ss_mfa_challenge_staff ON ss_mfa_challenges(tenant_id,staff_id,created_at);
CREATE INDEX IF NOT EXISTS ss_mfa_challenge_age ON ss_mfa_challenges(expires_at);
CREATE OR REPLACE FUNCTION ss_guard_mfa_session() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE staff_guard UUID;
DECLARE enabled_mfa BOOLEAN;
DECLARE current_version BIGINT;
BEGIN
    IF TG_OP='UPDATE' AND (NEW.tenant_id IS DISTINCT FROM OLD.tenant_id OR NEW.staff_id IS DISTINCT FROM OLD.staff_id OR
       NEW.token_hash IS DISTINCT FROM OLD.token_hash OR NEW.created_at IS DISTINCT FROM OLD.created_at) THEN
        RAISE EXCEPTION 'Session identity cannot be reassigned' USING ERRCODE='23514';
    END IF;
    -- UPDATE already owns a session row before this row trigger runs. Never
    -- wait for staff in that reverse order: fail closed and retry instead.
    BEGIN
        SELECT id INTO staff_guard FROM ss_staff
         WHERE tenant_id=NEW.tenant_id AND id=NEW.staff_id AND enabled=TRUE FOR UPDATE NOWAIT;
    EXCEPTION WHEN lock_not_available THEN
        RAISE EXCEPTION 'Account security changed concurrently; retry authentication' USING ERRCODE='40001';
    END;
    IF staff_guard IS NULL THEN RAISE EXCEPTION 'Enabled staff is required for a session' USING ERRCODE='23514'; END IF;
    SELECT enabled,version INTO enabled_mfa,current_version FROM ss_staff_mfa
     WHERE tenant_id=NEW.tenant_id AND staff_id=NEW.staff_id;
    IF enabled_mfa=TRUE AND (NEW.mfa_version IS DISTINCT FROM current_version OR NEW.mfa_authenticated_at IS NULL) THEN
        RAISE EXCEPTION 'Current second-factor proof is required for this session' USING ERRCODE='23514';
    END IF;
    RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS ss_session_mfa_guard ON ss_sessions;
CREATE TRIGGER ss_session_mfa_guard BEFORE INSERT OR UPDATE ON ss_sessions
    FOR EACH ROW EXECUTE FUNCTION ss_guard_mfa_session();
INSERT INTO ss_schema_versions(version) VALUES (4) ON CONFLICT DO NOTHING;

-- Version 5: immutable original physical-delivery evidence. No old receipt or
-- balance is classified/backfilled. Legacy drafts must bind real original
-- evidence before NEW acceptance; accepted evidence stays untouched.
CREATE TABLE IF NOT EXISTS ss_intake_claims (
    id UUID NOT NULL, tenant_id UUID NOT NULL REFERENCES ss_tenants(id),
    physical_reference TEXT NOT NULL CHECK(length(physical_reference) BETWEEN 3 AND 100),
    source_manifest JSONB NOT NULL CHECK(jsonb_typeof(source_manifest)='object'),
    manifest_hash TEXT NOT NULL CHECK(manifest_hash ~ '^[a-f0-9]{64}$'),
    semantic_hash TEXT NOT NULL CHECK(semantic_hash ~ '^[a-f0-9]{64}$'),
    raw_csv_sha256 TEXT CHECK(raw_csv_sha256 ~ '^[a-f0-9]{64}$'),
    additional_delivery_of UUID, additional_delivery_reason TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT 'open' CHECK(state IN ('open','consumed')),
    version BIGINT NOT NULL DEFAULT 0 CHECK(version>=0), bound_trade_id UUID,
    created_by UUID NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    consumed_at TIMESTAMPTZ, consumed_by UUID,
    PRIMARY KEY(tenant_id,id), UNIQUE(tenant_id,physical_reference),
    FOREIGN KEY(tenant_id,created_by) REFERENCES ss_staff(tenant_id,id),
    FOREIGN KEY(tenant_id,consumed_by) REFERENCES ss_staff(tenant_id,id),
    FOREIGN KEY(tenant_id,additional_delivery_of) REFERENCES ss_intake_claims(tenant_id,id),
    FOREIGN KEY(tenant_id,bound_trade_id) REFERENCES ss_trade_receipts(tenant_id,id),
    CHECK((additional_delivery_of IS NULL AND additional_delivery_reason='') OR
          (additional_delivery_of IS NOT NULL AND length(additional_delivery_reason) BETWEEN 10 AND 200)),
    CHECK((state='open' AND bound_trade_id IS NULL AND consumed_at IS NULL AND consumed_by IS NULL AND version=0) OR
          (state='consumed' AND bound_trade_id IS NOT NULL AND consumed_at IS NOT NULL AND consumed_by IS NOT NULL AND version=1))
);
CREATE UNIQUE INDEX IF NOT EXISTS ss_intake_original_semantic ON ss_intake_claims(tenant_id,semantic_hash) WHERE additional_delivery_of IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS ss_intake_original_csv ON ss_intake_claims(tenant_id,raw_csv_sha256) WHERE additional_delivery_of IS NULL AND raw_csv_sha256 IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS ss_intake_once_per_trade ON ss_intake_claims(tenant_id,bound_trade_id) WHERE bound_trade_id IS NOT NULL;
CREATE TABLE IF NOT EXISTS ss_intake_requests (
    tenant_id UUID NOT NULL REFERENCES ss_tenants(id), request_key TEXT NOT NULL CHECK(length(request_key) BETWEEN 1 AND 150),
    intent_hash TEXT NOT NULL CHECK(intent_hash ~ '^[a-f0-9]{64}$'), claim_id UUID NOT NULL,
    result JSONB NOT NULL CHECK(jsonb_typeof(result)='object'), created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY(tenant_id,request_key), FOREIGN KEY(tenant_id,claim_id) REFERENCES ss_intake_claims(tenant_id,id)
);
ALTER TABLE ss_trade_receipts ADD COLUMN IF NOT EXISTS physical_intake_id UUID;
ALTER TABLE ss_trade_receipts ADD COLUMN IF NOT EXISTS physical_manifest_hash TEXT;
ALTER TABLE ss_trade_receipt_lines ADD COLUMN IF NOT EXISTS source_line_key TEXT;
DO $$ BEGIN
    IF NOT EXISTS(SELECT 1 FROM pg_constraint WHERE conrelid='ss_trade_receipts'::regclass AND conname='ss_trade_physical_claim') THEN
        ALTER TABLE ss_trade_receipts ADD CONSTRAINT ss_trade_physical_claim FOREIGN KEY(tenant_id,physical_intake_id) REFERENCES ss_intake_claims(tenant_id,id);
        ALTER TABLE ss_trade_receipts ADD CONSTRAINT ss_trade_manifest_pair CHECK((physical_intake_id IS NULL AND physical_manifest_hash IS NULL) OR
             (physical_intake_id IS NOT NULL AND physical_manifest_hash IS NOT NULL AND physical_manifest_hash ~ '^[a-f0-9]{64}$'));
    END IF;
    IF NOT EXISTS(SELECT 1 FROM pg_constraint WHERE conrelid='ss_trade_receipt_lines'::regclass AND conname='ss_trade_source_line_key') THEN
        ALTER TABLE ss_trade_receipt_lines ADD CONSTRAINT ss_trade_source_line_key CHECK(source_line_key ~ '^[a-f0-9]{64}$');
    END IF;
END $$;
CREATE OR REPLACE FUNCTION ss_guard_physical_claim() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP='DELETE' OR OLD.state='consumed' THEN RAISE EXCEPTION 'Physical delivery evidence is immutable'; END IF;
    IF (to_jsonb(NEW)-ARRAY['state','version','bound_trade_id','consumed_at','consumed_by']) IS DISTINCT FROM
       (to_jsonb(OLD)-ARRAY['state','version','bound_trade_id','consumed_at','consumed_by']) THEN
        RAISE EXCEPTION 'Original physical delivery terms cannot change';
    END IF;
    RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS ss_physical_claim_guard ON ss_intake_claims;
CREATE TRIGGER ss_physical_claim_guard BEFORE UPDATE OR DELETE ON ss_intake_claims FOR EACH ROW EXECUTE FUNCTION ss_guard_physical_claim();
CREATE OR REPLACE FUNCTION ss_guard_trade_physical_claim() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE claim ss_intake_claims%ROWTYPE;
BEGIN
    IF OLD.physical_intake_id IS NOT NULL AND (NEW.physical_intake_id IS DISTINCT FROM OLD.physical_intake_id OR
       NEW.physical_manifest_hash IS DISTINCT FROM OLD.physical_manifest_hash) THEN RAISE EXCEPTION 'Trade physical binding cannot change'; END IF;
    IF OLD.state='draft' AND NEW.state IN ('accepted_credit','accepted_cash') THEN
        IF NEW.physical_intake_id IS NULL THEN RAISE EXCEPTION 'Central physical intake claim is required before new acceptance'; END IF;
        SELECT * INTO claim FROM ss_intake_claims WHERE tenant_id=NEW.tenant_id AND id=NEW.physical_intake_id FOR UPDATE;
        IF claim.state IS DISTINCT FROM 'consumed' OR claim.bound_trade_id IS DISTINCT FROM NEW.id OR
           claim.manifest_hash IS DISTINCT FROM NEW.physical_manifest_hash THEN RAISE EXCEPTION 'Physical intake was not exclusively consumed for this trade'; END IF;
    END IF;
    RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS ss_trade_physical_claim_guard ON ss_trade_receipts;
CREATE TRIGGER ss_trade_physical_claim_guard BEFORE UPDATE ON ss_trade_receipts FOR EACH ROW EXECUTE FUNCTION ss_guard_trade_physical_claim();
INSERT INTO ss_schema_versions(version) VALUES (5) ON CONFLICT DO NOTHING;

-- Version 6: hosted receiving prototype. No seller setting, mapping or template
-- is enabled/approved automatically; no legacy receipt or inventory is imported.
ALTER TABLE ss_oauth_states ADD COLUMN IF NOT EXISTS requested_profile TEXT NOT NULL DEFAULT 'standard';
ALTER TABLE ss_oauth_states ADD COLUMN IF NOT EXISTS application_id TEXT;
ALTER TABLE ss_oauth_states ADD COLUMN IF NOT EXISTS environment TEXT;
ALTER TABLE ss_oauth_states ADD COLUMN IF NOT EXISTS requested_scopes JSONB;
ALTER TABLE ss_square_connections ADD COLUMN IF NOT EXISTS requested_profile TEXT NOT NULL DEFAULT 'standard';
CREATE TABLE IF NOT EXISTS ss_receiving_settings (
 tenant_id UUID PRIMARY KEY REFERENCES ss_tenants(id), enabled BOOLEAN NOT NULL DEFAULT FALSE,
 new_price_enabled BOOLEAN NOT NULL DEFAULT FALSE, new_parent_enabled BOOLEAN NOT NULL DEFAULT FALSE
);
CREATE TABLE IF NOT EXISTS ss_receiving_binding_previews (
 tenant_id UUID NOT NULL REFERENCES ss_tenants(id), id UUID NOT NULL, facts JSONB NOT NULL,
 preview_hash TEXT NOT NULL CHECK(length(preview_hash)=64), created_by UUID NOT NULL,
 expires_at TIMESTAMPTZ NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 PRIMARY KEY(tenant_id,id), FOREIGN KEY(tenant_id,created_by) REFERENCES ss_staff(tenant_id,id)
);
CREATE TABLE IF NOT EXISTS ss_receiving_bindings (
 tenant_id UUID NOT NULL REFERENCES ss_tenants(id), id UUID NOT NULL, version BIGINT NOT NULL DEFAULT 1,
 identity JSONB NOT NULL, identity_hash TEXT NOT NULL CHECK(length(identity_hash)=64), provider JSONB NOT NULL,
 variation_id TEXT NOT NULL, parent_id TEXT NOT NULL, sku TEXT NOT NULL, location_id TEXT NOT NULL,
 selling_cents BIGINT NOT NULL CHECK(selling_cents BETWEEN 0 AND 100000000),
 identity_anchor_hash TEXT NOT NULL CHECK(length(identity_anchor_hash)=64), evidence_hash TEXT NOT NULL CHECK(length(evidence_hash)=64),
 approved_by UUID NOT NULL, approved_at TIMESTAMPTZ NOT NULL DEFAULT now(), approval_reason TEXT NOT NULL,
 revoked_at TIMESTAMPTZ, revoked_by UUID, revoke_reason TEXT,
 PRIMARY KEY(tenant_id,id), FOREIGN KEY(tenant_id,approved_by) REFERENCES ss_staff(tenant_id,id),
 FOREIGN KEY(tenant_id,revoked_by) REFERENCES ss_staff(tenant_id,id)
);
CREATE TABLE IF NOT EXISTS ss_receiving_price_template_previews (
 tenant_id UUID NOT NULL, id UUID NOT NULL, binding_id UUID NOT NULL, facts JSONB NOT NULL, preview_hash TEXT NOT NULL,
 created_by UUID NOT NULL, expires_at TIMESTAMPTZ NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 PRIMARY KEY(tenant_id,id), FOREIGN KEY(tenant_id,binding_id) REFERENCES ss_receiving_bindings(tenant_id,id),
 FOREIGN KEY(tenant_id,created_by) REFERENCES ss_staff(tenant_id,id)
);
CREATE TABLE IF NOT EXISTS ss_receiving_price_templates (
 tenant_id UUID NOT NULL, id UUID NOT NULL, version BIGINT NOT NULL DEFAULT 1, binding_id UUID NOT NULL,
 identity_hash TEXT NOT NULL CHECK(length(identity_hash)=64), parent_id TEXT NOT NULL, location_id TEXT NOT NULL,
 facts JSONB NOT NULL, approved BOOLEAN NOT NULL DEFAULT FALSE, approved_by UUID, approved_at TIMESTAMPTZ,
 approval_reason TEXT NOT NULL DEFAULT '', revoked_at TIMESTAMPTZ, revoked_by UUID, revoke_reason TEXT,
 PRIMARY KEY(tenant_id,id), FOREIGN KEY(tenant_id,binding_id) REFERENCES ss_receiving_bindings(tenant_id,id),
 FOREIGN KEY(tenant_id,approved_by) REFERENCES ss_staff(tenant_id,id), FOREIGN KEY(tenant_id,revoked_by) REFERENCES ss_staff(tenant_id,id),
 CHECK((NOT approved AND approved_by IS NULL AND approved_at IS NULL) OR
       (approved AND approved_by IS NOT NULL AND approved_at IS NOT NULL AND length(approval_reason) BETWEEN 10 AND 200))
);
ALTER TABLE ss_receiving_bindings ADD COLUMN IF NOT EXISTS approval_source TEXT NOT NULL DEFAULT 'owner_mapping';
ALTER TABLE ss_receiving_bindings ADD COLUMN IF NOT EXISTS template_id UUID;
CREATE UNIQUE INDEX IF NOT EXISTS ss_receiving_active_price_template ON ss_receiving_price_templates(tenant_id,identity_hash,parent_id,location_id) WHERE approved=TRUE AND revoked_at IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS ss_receiving_binding_active_identity ON ss_receiving_bindings(tenant_id,identity_hash,location_id,selling_cents) WHERE revoked_at IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS ss_receiving_binding_active_variation ON ss_receiving_bindings(tenant_id,variation_id,location_id) WHERE revoked_at IS NULL;
CREATE TABLE IF NOT EXISTS ss_receiving_operations (
 tenant_id UUID NOT NULL, id UUID NOT NULL, claim_id UUID NOT NULL, workflow_kind TEXT NOT NULL CHECK(workflow_kind IN ('accepted_trade','inventory_only')),
 revision BIGINT NOT NULL DEFAULT 0 CHECK(revision>=0), plan_hash TEXT NOT NULL CHECK(length(plan_hash)=64), plan JSONB NOT NULL,
 state TEXT NOT NULL DEFAULT 'ready' CHECK(state IN ('ready','cancelled_before_write','catalog_pending','catalog_confirmed','inventory_pending','outcome_unknown','needs_review','received')),
 first_admitted_at TIMESTAMPTZ, inventory_proof JSONB, receipt JSONB,
 lease_generation BIGINT NOT NULL DEFAULT 0, lease_until TIMESTAMPTZ,
 created_by UUID NOT NULL, updated_by UUID NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 PRIMARY KEY(tenant_id,id), UNIQUE(tenant_id,claim_id),
 FOREIGN KEY(tenant_id,claim_id) REFERENCES ss_intake_claims(tenant_id,id),
 FOREIGN KEY(tenant_id,created_by) REFERENCES ss_staff(tenant_id,id), FOREIGN KEY(tenant_id,updated_by) REFERENCES ss_staff(tenant_id,id),
 CHECK((state='received')=(receipt IS NOT NULL)), CHECK(first_admitted_at IS NOT NULL OR inventory_proof IS NULL)
);
ALTER TABLE ss_receiving_operations ADD COLUMN IF NOT EXISTS catalog_proof JSONB;
ALTER TABLE ss_receiving_operations ADD COLUMN IF NOT EXISTS catalog_resolutions JSONB;
ALTER TABLE ss_receiving_operations DROP CONSTRAINT IF EXISTS ss_receiving_operations_state_check;
ALTER TABLE ss_receiving_operations ADD CONSTRAINT ss_receiving_operations_state_check CHECK(state IN ('ready','cancelled_before_write','catalog_pending','catalog_confirmed','inventory_pending','outcome_unknown','needs_review','received'));
CREATE TABLE IF NOT EXISTS ss_receiving_plan_revisions (
 tenant_id UUID NOT NULL, operation_id UUID NOT NULL, revision BIGINT NOT NULL, plan_hash TEXT NOT NULL,
 plan JSONB NOT NULL, created_by UUID NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 PRIMARY KEY(tenant_id,operation_id,revision), FOREIGN KEY(tenant_id,operation_id) REFERENCES ss_receiving_operations(tenant_id,id)
);
CREATE TABLE IF NOT EXISTS ss_receiving_seller_lanes (
 environment TEXT NOT NULL, merchant_id TEXT NOT NULL, tenant_id UUID, operation_id UUID,
 state TEXT NOT NULL DEFAULT 'idle' CHECK(state IN ('idle','occupied','unknown')),
 PRIMARY KEY(environment,merchant_id), FOREIGN KEY(tenant_id,operation_id) REFERENCES ss_receiving_operations(tenant_id,id),
 CHECK((state='idle' AND tenant_id IS NULL AND operation_id IS NULL) OR (state<>'idle' AND tenant_id IS NOT NULL AND operation_id IS NOT NULL))
);
CREATE TABLE IF NOT EXISTS ss_receiving_steps (
 tenant_id UUID NOT NULL, operation_id UUID NOT NULL, ordinal INTEGER NOT NULL CHECK(ordinal IN (0,1)),
 endpoint TEXT NOT NULL CHECK(endpoint IN ('catalog_batch','inventory_batch')), api_version TEXT NOT NULL,
 body_hash TEXT NOT NULL CHECK(length(body_hash)=64), encrypted_body TEXT NOT NULL, provider_key UUID NOT NULL,
 occurred_at TIMESTAMPTZ NOT NULL, authorized_by UUID NOT NULL, session_hash TEXT NOT NULL,
 authority_hash TEXT NOT NULL, admitted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 PRIMARY KEY(tenant_id,operation_id,ordinal), UNIQUE(provider_key),
 FOREIGN KEY(tenant_id,operation_id) REFERENCES ss_receiving_operations(tenant_id,id),
 FOREIGN KEY(tenant_id,authorized_by) REFERENCES ss_staff(tenant_id,id)
);
ALTER TABLE ss_receiving_steps DROP CONSTRAINT IF EXISTS ss_receiving_steps_ordinal_check;
ALTER TABLE ss_receiving_steps ADD CONSTRAINT ss_receiving_steps_ordinal_check CHECK(ordinal IN (0,1));
ALTER TABLE ss_receiving_steps DROP CONSTRAINT IF EXISTS ss_receiving_steps_endpoint_check;
ALTER TABLE ss_receiving_steps ADD CONSTRAINT ss_receiving_steps_endpoint_check CHECK((ordinal=0 AND endpoint='catalog_batch') OR (ordinal=1 AND endpoint='inventory_batch'));
CREATE TABLE IF NOT EXISTS ss_receiving_requests (
 tenant_id UUID NOT NULL, request_key TEXT NOT NULL, action TEXT NOT NULL, intent_hash TEXT NOT NULL,
 operation_id UUID, result JSONB NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 PRIMARY KEY(tenant_id,request_key), FOREIGN KEY(tenant_id,operation_id) REFERENCES ss_receiving_operations(tenant_id,id)
);
CREATE TABLE IF NOT EXISTS ss_receiving_plan_intents (
 tenant_id UUID NOT NULL REFERENCES ss_tenants(id), request_key TEXT NOT NULL, intent_hash TEXT NOT NULL,
 wire_hash TEXT NOT NULL, claim_id UUID, created_by UUID NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 PRIMARY KEY(tenant_id,request_key), FOREIGN KEY(tenant_id,created_by) REFERENCES ss_staff(tenant_id,id),
 CHECK(length(intent_hash)=64 AND length(wire_hash)=64)
);
ALTER TABLE ss_receiving_plan_intents ADD COLUMN IF NOT EXISTS workflow_kind TEXT;
ALTER TABLE ss_receiving_plan_intents ADD COLUMN IF NOT EXISTS accepted_trade_id UUID;
ALTER TABLE ss_receiving_plan_intents ADD COLUMN IF NOT EXISTS accepted_receipt_hash TEXT;
DO $$ BEGIN
 IF NOT EXISTS(SELECT 1 FROM pg_constraint WHERE conrelid='ss_receiving_plan_intents'::regclass AND conname='ss_receiving_intent_workflow_check') THEN
  ALTER TABLE ss_receiving_plan_intents ADD CONSTRAINT ss_receiving_intent_workflow_check CHECK(
   (workflow_kind IS NULL AND accepted_trade_id IS NULL AND accepted_receipt_hash IS NULL) OR
   (workflow_kind='inventory_only' AND accepted_trade_id IS NULL AND accepted_receipt_hash IS NULL) OR
   (workflow_kind='accepted_trade' AND accepted_trade_id IS NOT NULL AND accepted_receipt_hash IS NOT NULL AND length(accepted_receipt_hash)=64));
 END IF;
END $$;
CREATE TABLE IF NOT EXISTS ss_receiving_attempts (
 id UUID PRIMARY KEY, tenant_id UUID NOT NULL, operation_id UUID NOT NULL, generation BIGINT NOT NULL,
 action TEXT NOT NULL, evidence JSONB NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 FOREIGN KEY(tenant_id,operation_id) REFERENCES ss_receiving_operations(tenant_id,id)
);
CREATE TABLE IF NOT EXISTS ss_receiving_request_outcomes (
 tenant_id UUID NOT NULL, request_key TEXT NOT NULL, result JSONB NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 PRIMARY KEY(tenant_id,request_key), FOREIGN KEY(tenant_id,request_key) REFERENCES ss_receiving_requests(tenant_id,request_key)
);
CREATE TABLE IF NOT EXISTS ss_receiving_exceptions (
 tenant_id UUID NOT NULL, operation_id UUID NOT NULL, code TEXT NOT NULL, occurrences BIGINT NOT NULL DEFAULT 1,
 first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(), last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 PRIMARY KEY(tenant_id,operation_id,code), FOREIGN KEY(tenant_id,operation_id) REFERENCES ss_receiving_operations(tenant_id,id)
);
CREATE OR REPLACE FUNCTION ss_guard_receiving_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'Receiving evidence is immutable'; END $$;
DO $$ DECLARE target TEXT; BEGIN
 FOREACH target IN ARRAY ARRAY['ss_receiving_steps','ss_receiving_plan_revisions','ss_receiving_attempts','ss_receiving_requests','ss_receiving_request_outcomes','ss_receiving_plan_intents'] LOOP
  EXECUTE format('DROP TRIGGER IF EXISTS ss_receiving_immutable ON %I',target);
  EXECUTE format('CREATE TRIGGER ss_receiving_immutable BEFORE UPDATE OR DELETE ON %I FOR EACH ROW EXECUTE FUNCTION ss_guard_receiving_immutable()',target);
 END LOOP;
END $$;
CREATE OR REPLACE FUNCTION ss_guard_receiving_operation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF TG_OP='DELETE' OR OLD.state='received' THEN RAISE EXCEPTION 'Receiving operation evidence must be retained'; END IF;
 IF NEW.tenant_id IS DISTINCT FROM OLD.tenant_id OR NEW.id IS DISTINCT FROM OLD.id OR NEW.claim_id IS DISTINCT FROM OLD.claim_id OR
    NEW.created_by IS DISTINCT FROM OLD.created_by OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN RAISE EXCEPTION 'Receiving identity is immutable'; END IF;
 IF OLD.first_admitted_at IS NOT NULL AND (NEW.first_admitted_at IS DISTINCT FROM OLD.first_admitted_at OR
    NEW.plan IS DISTINCT FROM OLD.plan OR NEW.plan_hash IS DISTINCT FROM OLD.plan_hash OR NEW.revision IS DISTINCT FROM OLD.revision OR
    NEW.workflow_kind IS DISTINCT FROM OLD.workflow_kind OR NEW.state='cancelled_before_write') THEN RAISE EXCEPTION 'Admitted receiving terms cannot change or release'; END IF;
 IF OLD.catalog_proof IS NOT NULL AND (NEW.catalog_proof IS DISTINCT FROM OLD.catalog_proof OR NEW.catalog_resolutions IS DISTINCT FROM OLD.catalog_resolutions) THEN
    RAISE EXCEPTION 'Confirmed catalog proof and resolutions are immutable';
 END IF;
 RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS ss_receiving_operation_guard ON ss_receiving_operations;
CREATE TRIGGER ss_receiving_operation_guard BEFORE UPDATE OR DELETE ON ss_receiving_operations FOR EACH ROW EXECUTE FUNCTION ss_guard_receiving_operation();
CREATE OR REPLACE FUNCTION ss_guard_receiving_binding() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF TG_OP='DELETE' OR OLD.revoked_at IS NOT NULL OR (to_jsonb(NEW)-ARRAY['revoked_at','revoked_by','revoke_reason']) IS DISTINCT FROM
    (to_jsonb(OLD)-ARRAY['revoked_at','revoked_by','revoke_reason']) THEN RAISE EXCEPTION 'Approved catalog binding evidence is immutable'; END IF;
 IF NEW.revoked_at IS NULL OR NEW.revoked_by IS NULL OR length(trim(NEW.revoke_reason))<10 THEN RAISE EXCEPTION 'Revocation requires explicit actor/time/reason'; END IF;
 RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS ss_receiving_binding_guard ON ss_receiving_bindings;
CREATE TRIGGER ss_receiving_binding_guard BEFORE UPDATE OR DELETE ON ss_receiving_bindings FOR EACH ROW EXECUTE FUNCTION ss_guard_receiving_binding();
DROP TRIGGER IF EXISTS ss_receiving_price_template_guard ON ss_receiving_price_templates;
CREATE TRIGGER ss_receiving_price_template_guard BEFORE UPDATE OR DELETE ON ss_receiving_price_templates FOR EACH ROW EXECUTE FUNCTION ss_guard_receiving_binding();
CREATE OR REPLACE FUNCTION ss_guard_trade_inventory_admission() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF OLD.state='draft' AND NEW.state IN ('accepted_cash','accepted_credit') AND EXISTS(
    SELECT 1 FROM ss_receiving_operations WHERE tenant_id=NEW.tenant_id AND claim_id=NEW.physical_intake_id
      AND workflow_kind='inventory_only' AND first_admitted_at IS NOT NULL) THEN
    RAISE EXCEPTION 'Inventory-only admission permanently blocks new trade acceptance on this claim';
 END IF;
 RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS ss_trade_inventory_admission_guard ON ss_trade_receipts;
CREATE TRIGGER ss_trade_inventory_admission_guard BEFORE UPDATE ON ss_trade_receipts FOR EACH ROW EXECUTE FUNCTION ss_guard_trade_inventory_admission();
INSERT INTO ss_schema_versions(version) VALUES (6) ON CONFLICT DO NOTHING;

-- Version 7: independent owner enrollment, issued only by platform operator.
-- No existing store/account/balance/OAuth record is created or inferred here.
CREATE TABLE IF NOT EXISTS ss_owner_enrollments (
 id UUID PRIMARY KEY, token_hash TEXT UNIQUE NOT NULL CHECK(token_hash ~ '^[a-f0-9]{64}$'),
 email TEXT NOT NULL CHECK(length(email) BETWEEN 3 AND 254),
 store_slug TEXT NOT NULL CHECK(store_slug ~ '^[a-z0-9][a-z0-9-]{2,49}$'),
 store_name TEXT NOT NULL CHECK(length(store_name) BETWEEN 1 AND 150),
 tenant_id UUID UNIQUE NOT NULL, owner_id UUID UNIQUE NOT NULL,
 operator_reference TEXT NOT NULL CHECK(length(operator_reference) BETWEEN 1 AND 100),
 issue_key TEXT NOT NULL CHECK(length(issue_key) BETWEEN 1 AND 150),
 issue_hash TEXT NOT NULL CHECK(issue_hash ~ '^[a-f0-9]{64}$'),
 issue_reason TEXT NOT NULL CHECK(length(issue_reason) BETWEEN 10 AND 500),
 purpose TEXT NOT NULL DEFAULT 'independent-store-owner-v1' CHECK(purpose='independent-store-owner-v1'),
 state TEXT NOT NULL DEFAULT 'issued' CHECK(state IN ('issued','consumed','revoked')),
 created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(), expires_at TIMESTAMPTZ NOT NULL,
 consumed_at TIMESTAMPTZ, accepted_key TEXT, accepted_hash TEXT, result JSONB,
 revoked_at TIMESTAMPTZ, revoked_by TEXT, revoke_reason TEXT,
 UNIQUE(operator_reference,issue_key), CHECK(expires_at>created_at),
 CHECK((state='issued' AND consumed_at IS NULL AND accepted_key IS NULL AND accepted_hash IS NULL AND result IS NULL AND revoked_at IS NULL AND revoked_by IS NULL AND revoke_reason IS NULL) OR
 (state='consumed' AND consumed_at IS NOT NULL AND accepted_key IS NOT NULL AND length(accepted_key) BETWEEN 1 AND 150 AND accepted_hash ~ '^[a-f0-9]{64}$' AND result IS NOT NULL AND jsonb_typeof(result)='object' AND revoked_at IS NULL AND revoked_by IS NULL AND revoke_reason IS NULL) OR
 (state='revoked' AND consumed_at IS NULL AND accepted_key IS NULL AND accepted_hash IS NULL AND result IS NULL AND revoked_at IS NOT NULL AND revoked_by IS NOT NULL AND length(trim(revoke_reason)) BETWEEN 10 AND 500))
);
CREATE UNIQUE INDEX IF NOT EXISTS ss_owner_enrollment_active_slug ON ss_owner_enrollments(store_slug) WHERE revoked_at IS NULL;
CREATE TABLE IF NOT EXISTS ss_owner_enrollment_audit (
 id UUID PRIMARY KEY, enrollment_id UUID NOT NULL REFERENCES ss_owner_enrollments(id),
 operator_reference TEXT NOT NULL, action TEXT NOT NULL CHECK(action IN ('issued','consumed','revoked')),
 at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
DROP TRIGGER IF EXISTS ss_owner_enrollment_audit_immutable ON ss_owner_enrollment_audit;
CREATE TRIGGER ss_owner_enrollment_audit_immutable BEFORE UPDATE OR DELETE ON ss_owner_enrollment_audit FOR EACH ROW EXECUTE FUNCTION ss_reject_immutable_evidence_update();
CREATE OR REPLACE FUNCTION ss_guard_owner_enrollment() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF TG_OP='DELETE' OR OLD.state<>'issued' THEN RAISE EXCEPTION 'Owner enrollment evidence is immutable'; END IF;
 IF (to_jsonb(NEW)-ARRAY['state','consumed_at','accepted_key','accepted_hash','result','revoked_at','revoked_by','revoke_reason']) IS DISTINCT FROM
    (to_jsonb(OLD)-ARRAY['state','consumed_at','accepted_key','accepted_hash','result','revoked_at','revoked_by','revoke_reason']) THEN
    RAISE EXCEPTION 'Owner invitation identity and scope are immutable';
 END IF;
 IF NEW.state NOT IN ('consumed','revoked') THEN RAISE EXCEPTION 'Owner invitation requires an explicit terminal transition'; END IF;
 IF NEW.state='consumed' THEN
   IF OLD.expires_at<=clock_timestamp() OR NOT EXISTS(
      SELECT 1 FROM ss_tenants t JOIN ss_staff s ON s.tenant_id=t.id
      WHERE t.id=OLD.tenant_id AND t.slug=OLD.store_slug AND t.name=OLD.store_name AND s.id=OLD.owner_id
        AND s.email=OLD.email AND s.role='owner' AND s.enabled=TRUE AND t.credit_checkout_enabled=FALSE) THEN
      RAISE EXCEPTION 'Current unexpired invitation and exact fresh store owner are required';
   END IF;
 END IF;
 RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS ss_owner_enrollment_guard ON ss_owner_enrollments;
CREATE TRIGGER ss_owner_enrollment_guard BEFORE UPDATE OR DELETE ON ss_owner_enrollments FOR EACH ROW EXECUTE FUNCTION ss_guard_owner_enrollment();
INSERT INTO ss_schema_versions(version) VALUES (7) ON CONFLICT DO NOTHING;

-- Version 8: signed revocation receipts and nondestructive authorization holds.
-- Receipt cursors are allocated ONLY under the application's environment notice
-- barrier; they order local committed notices, never provider token generations.
CREATE TABLE IF NOT EXISTS ss_square_revocation_receipts (
 notice_cursor BIGINT GENERATED ALWAYS AS IDENTITY UNIQUE,
 application_id TEXT NOT NULL, environment TEXT NOT NULL CHECK(environment IN ('sandbox','production')),
 event_id TEXT NOT NULL, merchant_id TEXT NOT NULL,
 body_hash TEXT NOT NULL CHECK(body_hash ~ '^[a-f0-9]{64}$'),
 outcome JSONB NOT NULL CHECK(jsonb_typeof(outcome)='object'),
 received_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY(application_id,environment,event_id)
);
CREATE INDEX IF NOT EXISTS ss_square_notice_scope_cursor ON ss_square_revocation_receipts(application_id,environment,notice_cursor DESC);
CREATE INDEX IF NOT EXISTS ss_square_notice_merchant_cursor ON ss_square_revocation_receipts(application_id,environment,merchant_id,notice_cursor DESC);
DROP TRIGGER IF EXISTS ss_square_revocation_receipt_immutable ON ss_square_revocation_receipts;
CREATE TRIGGER ss_square_revocation_receipt_immutable BEFORE UPDATE OR DELETE ON ss_square_revocation_receipts
 FOR EACH ROW EXECUTE FUNCTION ss_reject_immutable_evidence_update();
ALTER TABLE ss_square_connections ADD COLUMN IF NOT EXISTS authorization_generation UUID NOT NULL DEFAULT gen_random_uuid();
ALTER TABLE ss_square_connections ADD COLUMN IF NOT EXISTS pending_notice_cursor BIGINT REFERENCES ss_square_revocation_receipts(notice_cursor);
ALTER TABLE ss_oauth_states ADD COLUMN IF NOT EXISTS notice_cursor BIGINT CHECK(notice_cursor>=0);
ALTER TABLE ss_oauth_states ADD COLUMN IF NOT EXISTS expected_connection_generation UUID;
INSERT INTO ss_schema_versions(version) VALUES (8) ON CONFLICT DO NOTHING;

-- Version 9: fresh reviewed physical ownership is separate from original source
-- identity and SKU addressing. Old bindings stay NULL; no implicit migration.
-- The existing immutable-binding trigger includes this column automatically.
ALTER TABLE ss_receiving_bindings ADD COLUMN IF NOT EXISTS physical_ownership JSONB;
INSERT INTO ss_schema_versions(version) VALUES (9) ON CONFLICT DO NOTHING;

-- Schema 10: authenticated inventory evidence, not a continuous-zero observer.
-- Exact provider timestamp strings retain sub-microsecond precision. Local
-- received_at is the persistence clock, never substituted for provider time.
CREATE OR REPLACE FUNCTION ss_cleanup_timestamp_valid(value TEXT) RETURNS BOOLEAN
LANGUAGE plpgsql IMMUTABLE STRICT AS $$
BEGIN
    IF length(value)>35 OR value !~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,9})?(Z|[+-][0-9]{2}:[0-9]{2})$' THEN
        RETURN FALSE;
    END IF;
    -- Validate the local calendar separately: TIMESTAMPTZ's displacement range
    -- is narrower than RFC3339, and TIMESTAMP normalizes 24:00. Neither may
    -- silently change or reject a correctly bounded original wire timestamp.
    IF substring(value FROM 12 FOR 2)::INTEGER>23
       OR substring(value FROM 15 FOR 2)::INTEGER>59
       OR substring(value FROM 18 FOR 2)::INTEGER>59 THEN
        RETURN FALSE;
    END IF;
    IF right(value,1)<>'Z' THEN
        IF substring(value FROM length(value)-4 FOR 2)::INTEGER>23 OR right(value,2)::INTEGER>59 THEN
            RETURN FALSE;
        END IF;
    END IF;
    PERFORM left(value,19)::TIMESTAMP;
    RETURN TRUE;
EXCEPTION WHEN invalid_datetime_format OR datetime_field_overflow THEN
    RETURN FALSE;
END $$;

CREATE TABLE IF NOT EXISTS ss_cleanup_inventory_receipts (
    receipt_cursor BIGINT GENERATED ALWAYS AS IDENTITY UNIQUE,
    application_id TEXT NOT NULL CHECK (length(application_id) BETWEEN 1 AND 255 AND application_id=btrim(application_id)),
    environment TEXT NOT NULL CHECK (environment IN ('sandbox','production')),
    event_id TEXT NOT NULL CHECK (length(event_id) BETWEEN 1 AND 255 AND event_id=btrim(event_id)),
    merchant_id TEXT NOT NULL CHECK (length(merchant_id) BETWEEN 1 AND 255 AND merchant_id=btrim(merchant_id)),
    event_type TEXT NOT NULL CHECK (event_type='inventory.count.updated'),
    body_hash TEXT NOT NULL CHECK (body_hash ~ '^[0-9a-f]{64}$'),
    provider_created_at TEXT NOT NULL CHECK (ss_cleanup_timestamp_valid(provider_created_at)),
    resource_id TEXT NOT NULL CHECK (length(resource_id) BETWEEN 1 AND 255 AND resource_id=btrim(resource_id)),
    payload_status TEXT NOT NULL CHECK (payload_status IN ('counts_present','empty_counts','object_absent')),
    fact_count INTEGER NOT NULL CHECK (fact_count BETWEEN 0 AND 1000),
    tenant_id UUID REFERENCES ss_tenants(id),
    authorization_generation UUID,
    binding_status TEXT NOT NULL CHECK (binding_status IN ('active','unbound','environment_mismatch','revocation_pending','revoked','token_expired')),
    refresh_queued BOOLEAN NOT NULL,
    received_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY(application_id,environment,event_id),
    UNIQUE(receipt_cursor,application_id,environment,merchant_id,tenant_id,authorization_generation,binding_status),
    CHECK ((binding_status='unbound' AND tenant_id IS NULL AND authorization_generation IS NULL)
        OR (binding_status<>'unbound' AND tenant_id IS NOT NULL AND authorization_generation IS NOT NULL)),
    CHECK (refresh_queued=(binding_status='active')),
    CHECK ((payload_status='counts_present' AND fact_count>0) OR (payload_status<>'counts_present' AND fact_count=0))
);
CREATE INDEX IF NOT EXISTS ss_cleanup_inventory_receipt_scope
    ON ss_cleanup_inventory_receipts(application_id,environment,merchant_id,receipt_cursor);
CREATE INDEX IF NOT EXISTS ss_cleanup_inventory_receipt_generation
    ON ss_cleanup_inventory_receipts(tenant_id,authorization_generation,receipt_cursor);

CREATE TABLE IF NOT EXISTS ss_cleanup_inventory_facts (
    receipt_cursor BIGINT NOT NULL REFERENCES ss_cleanup_inventory_receipts(receipt_cursor),
    ordinal INTEGER NOT NULL CHECK (ordinal BETWEEN 0 AND 999),
    catalog_object_id TEXT NOT NULL CHECK (length(catalog_object_id) BETWEEN 1 AND 100 AND catalog_object_id=btrim(catalog_object_id)),
    catalog_object_type TEXT NOT NULL CHECK (catalog_object_type IN ('ITEM','ITEM_VARIATION')),
    location_id TEXT NOT NULL CHECK (length(location_id) BETWEEN 1 AND 100 AND location_id=btrim(location_id)),
    state TEXT NOT NULL CHECK (state IN ('CUSTOM','IN_STOCK','SOLD','RETURNED_BY_CUSTOMER','RESERVED_FOR_SALE',
        'SOLD_ONLINE','ORDERED_FROM_VENDOR','RECEIVED_FROM_VENDOR','IN_TRANSIT_TO','NONE','WASTE','UNLINKED_RETURN',
        'COMPOSED','DECOMPOSED','SUPPORTED_BY_NEWER_VERSION','IN_TRANSIT','UNTRACKED')),
    quantity TEXT NOT NULL CHECK (length(quantity) BETWEEN 1 AND 26 AND quantity ~ '^-?[0-9]+(\.[0-9]{1,5})?$'),
    provider_calculated_at TEXT NOT NULL CHECK (ss_cleanup_timestamp_valid(provider_calculated_at)),
    is_estimated BOOLEAN,
    PRIMARY KEY(receipt_cursor,ordinal)
);
CREATE INDEX IF NOT EXISTS ss_cleanup_inventory_fact_object
    ON ss_cleanup_inventory_facts(catalog_object_id,location_id,state,receipt_cursor);

-- An immutable per-receipt intent, NOT an executable lease/job. Historical
-- generation membership survives reconnect; it never follows the connection.
-- A future worker needs a separate reviewed claim/finish/highwater protocol.
CREATE TABLE IF NOT EXISTS ss_cleanup_refresh_queue (
    receipt_cursor BIGINT PRIMARY KEY,
    application_id TEXT NOT NULL,
    environment TEXT NOT NULL,
    merchant_id TEXT NOT NULL,
    tenant_id UUID NOT NULL,
    authorization_generation UUID NOT NULL,
    binding_status TEXT NOT NULL DEFAULT 'active' CHECK (binding_status='active'),
    state TEXT NOT NULL DEFAULT 'queued_not_executed' CHECK (state='queued_not_executed'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY(receipt_cursor,application_id,environment,merchant_id,tenant_id,authorization_generation,binding_status)
        REFERENCES ss_cleanup_inventory_receipts(receipt_cursor,application_id,environment,merchant_id,tenant_id,authorization_generation,binding_status)
);
CREATE INDEX IF NOT EXISTS ss_cleanup_refresh_generation
    ON ss_cleanup_refresh_queue(tenant_id,authorization_generation,receipt_cursor);

CREATE OR REPLACE FUNCTION ss_cleanup_fact_within_receipt() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE expected_count INTEGER;
BEGIN
    SELECT fact_count INTO expected_count FROM ss_cleanup_inventory_receipts WHERE receipt_cursor=NEW.receipt_cursor;
    IF expected_count IS NULL OR NEW.ordinal>=expected_count THEN
        RAISE EXCEPTION 'Inventory fact is outside its immutable receipt';
    END IF;
    RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS ss_cleanup_fact_within_receipt ON ss_cleanup_inventory_facts;
CREATE TRIGGER ss_cleanup_fact_within_receipt BEFORE INSERT ON ss_cleanup_inventory_facts
    FOR EACH ROW EXECUTE FUNCTION ss_cleanup_fact_within_receipt();

CREATE OR REPLACE FUNCTION ss_cleanup_receipt_complete() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF (SELECT count(*) FROM ss_cleanup_inventory_facts WHERE receipt_cursor=NEW.receipt_cursor)<>NEW.fact_count
       OR (SELECT count(*) FROM ss_cleanup_refresh_queue WHERE receipt_cursor=NEW.receipt_cursor)<>(CASE WHEN NEW.refresh_queued THEN 1 ELSE 0 END) THEN
        RAISE EXCEPTION 'Inventory receipt facts and refresh intent must commit together';
    END IF;
    RETURN NULL;
END $$;
DROP TRIGGER IF EXISTS ss_cleanup_receipt_complete ON ss_cleanup_inventory_receipts;
CREATE CONSTRAINT TRIGGER ss_cleanup_receipt_complete AFTER INSERT ON ss_cleanup_inventory_receipts
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION ss_cleanup_receipt_complete();

DROP TRIGGER IF EXISTS ss_cleanup_inventory_receipt_immutable ON ss_cleanup_inventory_receipts;
CREATE TRIGGER ss_cleanup_inventory_receipt_immutable BEFORE UPDATE OR DELETE ON ss_cleanup_inventory_receipts
    FOR EACH ROW EXECUTE FUNCTION ss_reject_immutable_evidence_update();
DROP TRIGGER IF EXISTS ss_cleanup_inventory_fact_immutable ON ss_cleanup_inventory_facts;
CREATE TRIGGER ss_cleanup_inventory_fact_immutable BEFORE UPDATE OR DELETE ON ss_cleanup_inventory_facts
    FOR EACH ROW EXECUTE FUNCTION ss_reject_immutable_evidence_update();
DROP TRIGGER IF EXISTS ss_cleanup_refresh_intent_immutable ON ss_cleanup_refresh_queue;
CREATE TRIGGER ss_cleanup_refresh_intent_immutable BEFORE UPDATE OR DELETE ON ss_cleanup_refresh_queue
    FOR EACH ROW EXECUTE FUNCTION ss_reject_immutable_evidence_update();
INSERT INTO ss_schema_versions(version) VALUES (10) ON CONFLICT DO NOTHING;

-- Schema 11: known local successful-token time/application binding and a private
-- renewal lease/cooldown. No inferred age/app or default backfill for old rows.
ALTER TABLE ss_square_connections ADD COLUMN IF NOT EXISTS oauth_application_id TEXT
    CHECK (length(oauth_application_id) BETWEEN 1 AND 255 AND oauth_application_id=btrim(oauth_application_id));
ALTER TABLE ss_square_connections ADD COLUMN IF NOT EXISTS token_obtained_at TIMESTAMPTZ;
ALTER TABLE ss_square_connections ADD COLUMN IF NOT EXISTS renewal_lease_token UUID;
ALTER TABLE ss_square_connections ADD COLUMN IF NOT EXISTS renewal_lease_until TIMESTAMPTZ;
ALTER TABLE ss_square_connections ADD COLUMN IF NOT EXISTS renewal_attempted_at TIMESTAMPTZ;
ALTER TABLE ss_square_connections ADD COLUMN IF NOT EXISTS renewal_retry_after TIMESTAMPTZ;
ALTER TABLE ss_square_connections ADD COLUMN IF NOT EXISTS renewal_error_code TEXT
    CHECK (renewal_error_code IN ('provider_failure','authority_changed'));
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='ss_square_connections'::regclass
                   AND conname='ss_square_renewal_lease_pair') THEN
        ALTER TABLE ss_square_connections ADD CONSTRAINT ss_square_renewal_lease_pair
            CHECK ((renewal_lease_token IS NULL)=(renewal_lease_until IS NULL));
    END IF;
END $$;
CREATE INDEX IF NOT EXISTS ss_square_renewal_due ON ss_square_connections(renewal_retry_after,renewal_attempted_at)
    WHERE revoked_at IS NULL AND pending_notice_cursor IS NULL;
INSERT INTO ss_schema_versions(version) VALUES (11) ON CONFLICT DO NOTHING;

-- Schema 12: diagnostic read history only. No zero_since, eligibility, deletion,
-- provider mutation, legacy observer or automatic activation is introduced.
CREATE TABLE IF NOT EXISTS ss_cleanup_observer_config (
    tenant_id UUID NOT NULL REFERENCES ss_tenants(id), application_id TEXT NOT NULL,
    environment TEXT NOT NULL CHECK(environment IN ('production','sandbox')), merchant_id TEXT NOT NULL,
    authorization_generation UUID NOT NULL, config_version BIGINT NOT NULL CHECK(config_version>0),
    enabled BOOLEAN NOT NULL DEFAULT FALSE, background_enabled BOOLEAN NOT NULL DEFAULT FALSE,
    cadence_seconds INTEGER NOT NULL CHECK(cadence_seconds BETWEEN 300 AND 86400),
    stale_after_seconds INTEGER NOT NULL CHECK(stale_after_seconds BETWEEN cadence_seconds AND 604800),
    owner_staff_id UUID NOT NULL, audit_id BIGINT NOT NULL REFERENCES ss_audit(id),
    request_key TEXT NOT NULL, request_hash TEXT NOT NULL CHECK(request_hash ~ '^[0-9a-f]{64}$'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY(tenant_id,application_id,environment,merchant_id,authorization_generation,config_version),
    UNIQUE(tenant_id,request_key), FOREIGN KEY(tenant_id,owner_staff_id) REFERENCES ss_staff(tenant_id,id),
    CHECK(NOT background_enabled OR enabled)
);
CREATE TABLE IF NOT EXISTS ss_cleanup_collection_work (
    id UUID PRIMARY KEY, tenant_id UUID NOT NULL, application_id TEXT NOT NULL, environment TEXT NOT NULL,
    merchant_id TEXT NOT NULL, authorization_generation UUID NOT NULL, config_version BIGINT NOT NULL,
    receipt_hwm BIGINT NOT NULL CHECK(receipt_hwm>=0),
    receipt_manifest JSONB NOT NULL CHECK(jsonb_typeof(receipt_manifest)='array' AND jsonb_array_length(receipt_manifest)<=10000),
    manifest_hash TEXT NOT NULL CHECK(manifest_hash ~ '^[0-9a-f]{64}$'),
    trigger_kind TEXT NOT NULL CHECK(trigger_kind IN ('manual','notification','periodic')),
    principal_kind TEXT NOT NULL CHECK(principal_kind IN ('manual','platform')),
    staff_id UUID, session_hash TEXT, requested_role TEXT,
    state TEXT NOT NULL DEFAULT 'queued' CHECK(state IN ('queued','reading','done','blocked','superseded')),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts BETWEEN 0 AND 100),
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(), lease_token UUID, lease_until TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(), finished_at TIMESTAMPTZ,
    FOREIGN KEY(tenant_id,application_id,environment,merchant_id,authorization_generation,config_version)
        REFERENCES ss_cleanup_observer_config(tenant_id,application_id,environment,merchant_id,authorization_generation,config_version),
    FOREIGN KEY(tenant_id,staff_id) REFERENCES ss_staff(tenant_id,id),
    UNIQUE(id,tenant_id,application_id,environment,merchant_id,authorization_generation,config_version,receipt_hwm,manifest_hash),
    CHECK((principal_kind='platform' AND staff_id IS NULL AND session_hash IS NULL AND requested_role IS NULL AND trigger_kind<>'manual')
       OR (principal_kind='manual' AND staff_id IS NOT NULL AND session_hash IS NOT NULL AND requested_role IS NOT NULL AND session_hash ~ '^[0-9a-f]{64}$' AND requested_role IN ('owner','manager') AND trigger_kind='manual')),
    CHECK((state='reading' AND lease_token IS NOT NULL AND lease_until IS NOT NULL)
       OR (state<>'reading' AND lease_token IS NULL AND lease_until IS NULL))
);
CREATE UNIQUE INDEX IF NOT EXISTS ss_cleanup_work_active_scope
    ON ss_cleanup_collection_work(tenant_id,application_id,environment,merchant_id,authorization_generation)
    WHERE state IN ('queued','reading');
CREATE INDEX IF NOT EXISTS ss_cleanup_work_due ON ss_cleanup_collection_work(next_attempt_at,created_at,id)
    WHERE state IN ('queued','reading');

CREATE TABLE IF NOT EXISTS ss_cleanup_manual_requests (
    tenant_id UUID NOT NULL, request_key TEXT NOT NULL, request_hash TEXT NOT NULL CHECK(request_hash ~ '^[0-9a-f]{64}$'),
    work_id UUID NOT NULL, application_id TEXT NOT NULL, environment TEXT NOT NULL, merchant_id TEXT NOT NULL,
    authorization_generation UUID NOT NULL, config_version BIGINT NOT NULL, receipt_hwm BIGINT NOT NULL, manifest_hash TEXT NOT NULL,
    staff_id UUID NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY(tenant_id,request_key), FOREIGN KEY(tenant_id,staff_id) REFERENCES ss_staff(tenant_id,id),
    FOREIGN KEY(work_id,tenant_id,application_id,environment,merchant_id,authorization_generation,config_version,receipt_hwm,manifest_hash)
        REFERENCES ss_cleanup_collection_work(id,tenant_id,application_id,environment,merchant_id,authorization_generation,config_version,receipt_hwm,manifest_hash)
);
CREATE TABLE IF NOT EXISTS ss_cleanup_collection_attempts (
    id UUID PRIMARY KEY, attempt_cursor BIGINT GENERATED ALWAYS AS IDENTITY UNIQUE,
    work_id UUID NOT NULL, attempt INTEGER NOT NULL CHECK(attempt BETWEEN 1 AND 100),
    tenant_id UUID NOT NULL, application_id TEXT NOT NULL, environment TEXT NOT NULL, merchant_id TEXT NOT NULL,
    authorization_generation UUID NOT NULL, config_version BIGINT NOT NULL, receipt_hwm BIGINT NOT NULL, manifest_hash TEXT NOT NULL,
    notice_cursor BIGINT NOT NULL CHECK(notice_cursor>=0), access_digest TEXT NOT NULL CHECK(access_digest ~ '^[0-9a-f]{64}$'),
    started_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(), lease_until TIMESTAMPTZ NOT NULL,
    UNIQUE(work_id,attempt),
    UNIQUE(id,work_id,tenant_id,application_id,environment,merchant_id,authorization_generation,config_version,receipt_hwm,manifest_hash),
    FOREIGN KEY(work_id,tenant_id,application_id,environment,merchant_id,authorization_generation,config_version,receipt_hwm,manifest_hash)
        REFERENCES ss_cleanup_collection_work(id,tenant_id,application_id,environment,merchant_id,authorization_generation,config_version,receipt_hwm,manifest_hash)
);
CREATE INDEX IF NOT EXISTS ss_cleanup_attempt_history ON ss_cleanup_collection_attempts
    (tenant_id,application_id,environment,merchant_id,authorization_generation,attempt_cursor DESC);
CREATE TABLE IF NOT EXISTS ss_cleanup_collections (
    id UUID PRIMARY KEY, attempt_id UUID NOT NULL UNIQUE, work_id UUID NOT NULL,
    tenant_id UUID NOT NULL, application_id TEXT NOT NULL, environment TEXT NOT NULL, merchant_id TEXT NOT NULL,
    authorization_generation UUID NOT NULL, config_version BIGINT NOT NULL, receipt_hwm BIGINT NOT NULL, manifest_hash TEXT NOT NULL,
    result_hash TEXT NOT NULL CHECK(result_hash ~ '^[0-9a-f]{64}$'),
    outcome TEXT NOT NULL CHECK(outcome IN ('complete','partial','fenced')),
    started_at TIMESTAMPTZ NOT NULL, finished_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    coverage_complete BOOLEAN NOT NULL, pagination_complete BOOLEAN NOT NULL, context_unchanged BOOLEAN NOT NULL, bracketed BOOLEAN NOT NULL,
    reason_codes JSONB NOT NULL CHECK(jsonb_typeof(reason_codes)='array' AND jsonb_array_length(reason_codes)<=32),
    before_context_hash TEXT CHECK(before_context_hash ~ '^[0-9a-f]{64}$'), after_context_hash TEXT CHECK(after_context_hash ~ '^[0-9a-f]{64}$'),
    context JSONB NOT NULL CHECK(jsonb_typeof(context)='object' AND octet_length(context::TEXT)<=16777216),
    missing JSONB CHECK(missing IS NULL OR (jsonb_typeof(missing)='array' AND jsonb_array_length(missing)<=100000)),
    missing_count INTEGER CHECK(missing_count>=0), count_rows INTEGER NOT NULL CHECK(count_rows BETWEEN 0 AND 100000),
    nonzero_count INTEGER NOT NULL CHECK(nonzero_count BETWEEN 0 AND count_rows),
    receipt_flags JSONB NOT NULL CHECK(jsonb_typeof(receipt_flags)='array' AND jsonb_array_length(receipt_flags)<=16),
    UNIQUE(id,tenant_id,application_id,environment,merchant_id,authorization_generation),
    FOREIGN KEY(attempt_id,work_id,tenant_id,application_id,environment,merchant_id,authorization_generation,config_version,receipt_hwm,manifest_hash)
        REFERENCES ss_cleanup_collection_attempts(id,work_id,tenant_id,application_id,environment,merchant_id,authorization_generation,config_version,receipt_hwm,manifest_hash),
    CHECK(coverage_complete=(outcome='complete')),
    CHECK(NOT coverage_complete OR (pagination_complete AND context_unchanged AND bracketed AND missing_count IS NOT NULL AND missing_count=0 AND missing='[]'::JSONB)),
    CHECK(outcome<>'fenced' OR (count_rows=0 AND nonzero_count=0 AND context='{}'::JSONB AND before_context_hash IS NULL AND after_context_hash IS NULL))
);
CREATE TABLE IF NOT EXISTS ss_cleanup_collection_counts (
    collection_id UUID NOT NULL REFERENCES ss_cleanup_collections(id), ordinal INTEGER NOT NULL CHECK(ordinal BETWEEN 0 AND 99999),
    catalog_object_id TEXT NOT NULL CHECK(length(catalog_object_id) BETWEEN 1 AND 100),
    location_id TEXT NOT NULL CHECK(length(location_id) BETWEEN 1 AND 100),
    catalog_object_type TEXT CHECK(catalog_object_type='ITEM_VARIATION'), state TEXT NOT NULL CHECK(state='IN_STOCK'),
    quantity TEXT NOT NULL CHECK(length(quantity) BETWEEN 1 AND 26 AND quantity ~ '^[+-]?[0-9]+(\.[0-9]{1,5})?$'),
    calculated_at TEXT CHECK(ss_cleanup_timestamp_valid(calculated_at)), is_estimated BOOLEAN,
    PRIMARY KEY(collection_id,ordinal)
);
CREATE TABLE IF NOT EXISTS ss_cleanup_current_projection (
    tenant_id UUID NOT NULL, application_id TEXT NOT NULL, environment TEXT NOT NULL, merchant_id TEXT NOT NULL,
    authorization_generation UUID NOT NULL, config_version BIGINT NOT NULL,
    latest_collection_id UUID, last_complete_id UUID, acknowledged_hwm BIGINT NOT NULL DEFAULT 0 CHECK(acknowledged_hwm>=0),
    diagnostic_epoch UUID NOT NULL DEFAULT gen_random_uuid(),
    health TEXT NOT NULL CHECK(health IN ('disabled','complete','partial','stale','authority_blocked')),
    reason_codes JSONB NOT NULL DEFAULT '[]' CHECK(jsonb_typeof(reason_codes)='array' AND jsonb_array_length(reason_codes)<=32),
    context_hash TEXT CHECK(context_hash ~ '^[0-9a-f]{64}$'), next_due_at TIMESTAMPTZ,
    PRIMARY KEY(tenant_id,application_id,environment,merchant_id,authorization_generation),
    FOREIGN KEY(tenant_id,application_id,environment,merchant_id,authorization_generation,config_version)
        REFERENCES ss_cleanup_observer_config(tenant_id,application_id,environment,merchant_id,authorization_generation,config_version),
    FOREIGN KEY(latest_collection_id,tenant_id,application_id,environment,merchant_id,authorization_generation)
        REFERENCES ss_cleanup_collections(id,tenant_id,application_id,environment,merchant_id,authorization_generation),
    FOREIGN KEY(last_complete_id,tenant_id,application_id,environment,merchant_id,authorization_generation)
        REFERENCES ss_cleanup_collections(id,tenant_id,application_id,environment,merchant_id,authorization_generation)
);
CREATE TABLE IF NOT EXISTS ss_cleanup_work_events (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY, work_id UUID NOT NULL REFERENCES ss_cleanup_collection_work(id),
    attempt_id UUID REFERENCES ss_cleanup_collection_attempts(id),
    reason_code TEXT NOT NULL CHECK(reason_code ~ '^[a-z][a-z0-9_]{0,63}$'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
CREATE OR REPLACE FUNCTION ss_cleanup_projection_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.last_complete_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM ss_cleanup_collections
        WHERE id=NEW.last_complete_id AND coverage_complete) THEN
        RAISE EXCEPTION 'Last complete cleanup read must reference complete coverage';
    END IF;
    IF NEW.health='complete' AND (NEW.latest_collection_id IS NULL OR NOT EXISTS(SELECT 1 FROM ss_cleanup_collections
        WHERE id=NEW.latest_collection_id AND coverage_complete AND config_version=NEW.config_version)) THEN
        RAISE EXCEPTION 'Current complete cleanup projection requires matching complete evidence';
    END IF;
    RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS ss_cleanup_projection_guard ON ss_cleanup_current_projection;
CREATE TRIGGER ss_cleanup_projection_guard BEFORE INSERT OR UPDATE ON ss_cleanup_current_projection
    FOR EACH ROW EXECUTE FUNCTION ss_cleanup_projection_guard();
CREATE OR REPLACE FUNCTION ss_cleanup_work_identity_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF ROW(NEW.id,NEW.tenant_id,NEW.application_id,NEW.environment,NEW.merchant_id,NEW.authorization_generation,
           NEW.config_version,NEW.receipt_hwm,NEW.receipt_manifest,NEW.manifest_hash,NEW.trigger_kind,NEW.principal_kind,
           NEW.staff_id,NEW.session_hash,NEW.requested_role,NEW.created_at)
       IS DISTINCT FROM ROW(OLD.id,OLD.tenant_id,OLD.application_id,OLD.environment,OLD.merchant_id,OLD.authorization_generation,
           OLD.config_version,OLD.receipt_hwm,OLD.receipt_manifest,OLD.manifest_hash,OLD.trigger_kind,OLD.principal_kind,
           OLD.staff_id,OLD.session_hash,OLD.requested_role,OLD.created_at) THEN
        RAISE EXCEPTION 'Original cleanup work scope and principal cannot be changed';
    END IF;
    RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS ss_cleanup_work_identity ON ss_cleanup_collection_work;
CREATE TRIGGER ss_cleanup_work_identity BEFORE UPDATE ON ss_cleanup_collection_work FOR EACH ROW EXECUTE FUNCTION ss_cleanup_work_identity_guard();
DROP TRIGGER IF EXISTS ss_cleanup_work_no_delete ON ss_cleanup_collection_work;
CREATE TRIGGER ss_cleanup_work_no_delete BEFORE DELETE ON ss_cleanup_collection_work FOR EACH ROW EXECUTE FUNCTION ss_reject_immutable_evidence_update();

CREATE OR REPLACE FUNCTION ss_cleanup_collection_complete() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF (SELECT count(*) FROM ss_cleanup_collection_counts WHERE collection_id=NEW.id)<>NEW.count_rows THEN
        RAISE EXCEPTION 'Collection counts must commit with their immutable collection';
    END IF;
    RETURN NULL;
END $$;
DROP TRIGGER IF EXISTS ss_cleanup_collection_complete ON ss_cleanup_collections;
CREATE CONSTRAINT TRIGGER ss_cleanup_collection_complete AFTER INSERT ON ss_cleanup_collections
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION ss_cleanup_collection_complete();
CREATE OR REPLACE FUNCTION ss_cleanup_collection_count_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE expected_count INTEGER;
BEGIN
    SELECT count_rows INTO expected_count FROM ss_cleanup_collections WHERE id=NEW.collection_id;
    IF expected_count IS NULL OR NEW.ordinal>=expected_count THEN RAISE EXCEPTION 'Count is outside its immutable collection'; END IF;
    RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS ss_cleanup_collection_count_guard ON ss_cleanup_collection_counts;
CREATE TRIGGER ss_cleanup_collection_count_guard BEFORE INSERT ON ss_cleanup_collection_counts
    FOR EACH ROW EXECUTE FUNCTION ss_cleanup_collection_count_guard();
DO $$ DECLARE table_name TEXT; BEGIN
    FOREACH table_name IN ARRAY ARRAY['ss_cleanup_observer_config','ss_cleanup_manual_requests','ss_cleanup_collection_attempts','ss_cleanup_collections','ss_cleanup_collection_counts','ss_cleanup_work_events'] LOOP
        EXECUTE format('DROP TRIGGER IF EXISTS ss_cleanup_evidence_immutable ON %I',table_name);
        EXECUTE format('CREATE TRIGGER ss_cleanup_evidence_immutable BEFORE UPDATE OR DELETE ON %I FOR EACH ROW EXECUTE FUNCTION ss_reject_immutable_evidence_update()',table_name);
    END LOOP;
END $$;
INSERT INTO ss_schema_versions(version) VALUES (12) ON CONFLICT DO NOTHING;

-- Schema 13: bounded local receipt processing, separate from read coverage.
CREATE INDEX IF NOT EXISTS ss_cleanup_receipt_full_scope_seek ON ss_cleanup_inventory_receipts
    (tenant_id,application_id,environment,merchant_id,authorization_generation,receipt_cursor)
    WHERE binding_status IN ('active','revoked','revocation_pending','token_expired');
CREATE INDEX IF NOT EXISTS ss_cleanup_intent_full_scope_seek ON ss_cleanup_refresh_queue
    (tenant_id,application_id,environment,merchant_id,authorization_generation,receipt_cursor);
CREATE OR REPLACE FUNCTION ss_cleanup_counter_shape(value JSONB) RETURNS BOOLEAN LANGUAGE plpgsql IMMUTABLE STRICT AS $$
DECLARE field TEXT;
BEGIN
    IF jsonb_typeof(value)<>'object' OR octet_length(value::TEXT)>4096 THEN RETURN FALSE; END IF;
    FOREACH field IN ARRAY ARRAY['receipts','facts','positive','negative','estimated','item','other_state','unknown_payload','interrupted_authority'] LOOP
        IF jsonb_typeof(value->field) IS DISTINCT FROM 'number' OR (value->>field) !~ '^(0|[1-9][0-9]{0,29})$' THEN RETURN FALSE; END IF;
    END LOOP;
    RETURN (SELECT count(*) FROM jsonb_object_keys(value))=9;
END $$;
CREATE TABLE IF NOT EXISTS ss_cleanup_receipt_batches (
    id UUID PRIMARY KEY, tenant_id UUID NOT NULL, application_id TEXT NOT NULL, environment TEXT NOT NULL,
    merchant_id TEXT NOT NULL, authorization_generation UUID NOT NULL, config_version BIGINT NOT NULL,
    is_genesis BOOLEAN NOT NULL, previous_id UUID, previous_hash TEXT,
    low_exclusive BIGINT NOT NULL CHECK(low_exclusive>=0), high_inclusive BIGINT NOT NULL CHECK(high_inclusive>=low_exclusive),
    receipt_manifest JSONB NOT NULL CHECK(jsonb_typeof(receipt_manifest)='array' AND jsonb_array_length(receipt_manifest)<=100),
    manifest_hash TEXT NOT NULL CHECK(manifest_hash ~ '^[0-9a-f]{64}$'),
    delta_counts JSONB NOT NULL CHECK(ss_cleanup_counter_shape(delta_counts)), prefix_counts JSONB NOT NULL CHECK(ss_cleanup_counter_shape(prefix_counts)),
    chain_hash TEXT NOT NULL CHECK(chain_hash ~ '^[0-9a-f]{64}$'), created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    UNIQUE(id,tenant_id,application_id,environment,merchant_id,authorization_generation),
    UNIQUE(id,tenant_id,application_id,environment,merchant_id,authorization_generation,chain_hash,high_inclusive),
    UNIQUE(id,tenant_id,application_id,environment,merchant_id,authorization_generation,chain_hash),
    FOREIGN KEY(tenant_id,application_id,environment,merchant_id,authorization_generation,config_version)
        REFERENCES ss_cleanup_observer_config(tenant_id,application_id,environment,merchant_id,authorization_generation,config_version),
    FOREIGN KEY(previous_id,tenant_id,application_id,environment,merchant_id,authorization_generation,previous_hash,low_exclusive)
        REFERENCES ss_cleanup_receipt_batches(id,tenant_id,application_id,environment,merchant_id,authorization_generation,chain_hash,high_inclusive),
    CHECK((is_genesis AND previous_id IS NULL AND previous_hash IS NULL AND low_exclusive=0 AND high_inclusive=0)
       OR (NOT is_genesis AND previous_id IS NOT NULL AND previous_hash IS NOT NULL AND high_inclusive>low_exclusive))
);
CREATE UNIQUE INDEX IF NOT EXISTS ss_cleanup_batch_genesis ON ss_cleanup_receipt_batches
    (tenant_id,application_id,environment,merchant_id,authorization_generation) WHERE is_genesis;
CREATE UNIQUE INDEX IF NOT EXISTS ss_cleanup_batch_successor ON ss_cleanup_receipt_batches(previous_id) WHERE previous_id IS NOT NULL;
CREATE TABLE IF NOT EXISTS ss_cleanup_receipt_progress (
    tenant_id UUID NOT NULL, application_id TEXT NOT NULL, environment TEXT NOT NULL, merchant_id TEXT NOT NULL, authorization_generation UUID NOT NULL,
    head_id UUID NOT NULL, head_hash TEXT NOT NULL, processed_hwm BIGINT NOT NULL CHECK(processed_hwm>=0), revision BIGINT NOT NULL DEFAULT 0 CHECK(revision>=0),
    PRIMARY KEY(tenant_id,application_id,environment,merchant_id,authorization_generation),
    FOREIGN KEY(head_id,tenant_id,application_id,environment,merchant_id,authorization_generation,head_hash,processed_hwm)
        REFERENCES ss_cleanup_receipt_batches(id,tenant_id,application_id,environment,merchant_id,authorization_generation,chain_hash,high_inclusive)
);
CREATE OR REPLACE FUNCTION ss_cleanup_batch_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE previous ss_cleanup_receipt_batches%ROWTYPE;
DECLARE expected_manifest JSONB;
DECLARE expected_delta JSONB;
DECLARE expected_prefix JSONB;
DECLARE ids BIGINT[];
DECLARE field TEXT;
BEGIN
    IF NEW.is_genesis THEN
        expected_delta='{"receipts":0,"facts":0,"positive":0,"negative":0,"estimated":0,"item":0,"other_state":0,"unknown_payload":0,"interrupted_authority":0}'::JSONB;
        IF NEW.receipt_manifest<>'[]'::JSONB OR NEW.delta_counts<>expected_delta OR NEW.prefix_counts<>expected_delta THEN
            RAISE EXCEPTION 'Genesis cannot invent receipt processing';
        END IF;
        RETURN NEW;
    END IF;
    SELECT * INTO previous FROM ss_cleanup_receipt_batches WHERE id=NEW.previous_id;
    IF previous.id IS NULL OR previous.chain_hash<>NEW.previous_hash OR previous.high_inclusive<>NEW.low_exclusive THEN
        RAISE EXCEPTION 'Receipt batch predecessor changed';
    END IF;
    SELECT COALESCE(jsonb_agg(jsonb_build_object('receipt_cursor',r.receipt_cursor,'body_hash',r.body_hash) ORDER BY r.receipt_cursor),'[]'::JSONB),
           array_agg(r.receipt_cursor ORDER BY r.receipt_cursor),
           jsonb_build_object('receipts',count(*),'unknown_payload',count(*) FILTER(WHERE r.payload_status<>'counts_present'),
               'interrupted_authority',count(*) FILTER(WHERE r.binding_status<>'active'))
      INTO expected_manifest,ids,expected_delta FROM (
        SELECT receipt_cursor,body_hash,payload_status,binding_status FROM ss_cleanup_inventory_receipts
        WHERE tenant_id=NEW.tenant_id AND application_id=NEW.application_id AND environment=NEW.environment
          AND merchant_id=NEW.merchant_id AND authorization_generation=NEW.authorization_generation
          AND binding_status IN ('active','revoked','revocation_pending','token_expired')
          AND receipt_cursor>NEW.low_exclusive AND receipt_cursor<=NEW.high_inclusive ORDER BY receipt_cursor LIMIT 101
      ) r;
    IF jsonb_array_length(expected_manifest)=0 OR jsonb_array_length(expected_manifest)>100 OR expected_manifest<>NEW.receipt_manifest
       OR (expected_manifest->-1->>'receipt_cursor')::BIGINT<>NEW.high_inclusive THEN
        RAISE EXCEPTION 'Receipt batch must contain its exact bounded scoped prefix';
    END IF;
    SELECT expected_delta || jsonb_build_object('facts',count(*),'positive',count(*) FILTER(WHERE quantity::NUMERIC>0),
        'negative',count(*) FILTER(WHERE quantity::NUMERIC<0),'estimated',count(*) FILTER(WHERE is_estimated IS TRUE),
        'item',count(*) FILTER(WHERE catalog_object_type='ITEM'),'other_state',count(*) FILTER(WHERE state<>'IN_STOCK'))
      INTO expected_delta FROM ss_cleanup_inventory_facts WHERE receipt_cursor=ANY(ids);
    expected_prefix=previous.prefix_counts;
    FOREACH field IN ARRAY ARRAY['receipts','facts','positive','negative','estimated','item','other_state','unknown_payload','interrupted_authority'] LOOP
        expected_prefix=jsonb_set(expected_prefix,ARRAY[field],to_jsonb((previous.prefix_counts->>field)::NUMERIC+(expected_delta->>field)::NUMERIC));
    END LOOP;
    IF expected_delta<>NEW.delta_counts OR expected_prefix<>NEW.prefix_counts THEN RAISE EXCEPTION 'Receipt prefix counts cannot omit witnesses'; END IF;
    RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS ss_cleanup_batch_guard ON ss_cleanup_receipt_batches;
CREATE TRIGGER ss_cleanup_batch_guard BEFORE INSERT ON ss_cleanup_receipt_batches FOR EACH ROW EXECUTE FUNCTION ss_cleanup_batch_guard();
DROP TRIGGER IF EXISTS ss_cleanup_batch_immutable ON ss_cleanup_receipt_batches;
CREATE TRIGGER ss_cleanup_batch_immutable BEFORE UPDATE OR DELETE ON ss_cleanup_receipt_batches FOR EACH ROW EXECUTE FUNCTION ss_reject_immutable_evidence_update();
CREATE OR REPLACE FUNCTION ss_cleanup_progress_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE node ss_cleanup_receipt_batches%ROWTYPE;
BEGIN
    SELECT * INTO node FROM ss_cleanup_receipt_batches WHERE id=NEW.head_id;
    IF TG_OP='INSERT' THEN
        IF NOT node.is_genesis OR NEW.processed_hwm<>0 OR NEW.revision<>0 THEN RAISE EXCEPTION 'Receipt progress must start at genesis'; END IF;
    ELSE
        IF ROW(NEW.tenant_id,NEW.application_id,NEW.environment,NEW.merchant_id,NEW.authorization_generation)
           IS DISTINCT FROM ROW(OLD.tenant_id,OLD.application_id,OLD.environment,OLD.merchant_id,OLD.authorization_generation)
           OR node.previous_id IS DISTINCT FROM OLD.head_id OR node.previous_hash IS DISTINCT FROM OLD.head_hash
           OR node.low_exclusive<>OLD.processed_hwm OR NEW.revision<>OLD.revision+1 OR NEW.processed_hwm<=OLD.processed_hwm THEN
            RAISE EXCEPTION 'Receipt progress may advance only to its direct immutable successor';
        END IF;
    END IF;
    RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS ss_cleanup_progress_guard ON ss_cleanup_receipt_progress;
CREATE TRIGGER ss_cleanup_progress_guard BEFORE INSERT OR UPDATE ON ss_cleanup_receipt_progress FOR EACH ROW EXECUTE FUNCTION ss_cleanup_progress_guard();
DROP TRIGGER IF EXISTS ss_cleanup_progress_no_delete ON ss_cleanup_receipt_progress;
CREATE TRIGGER ss_cleanup_progress_no_delete BEFORE DELETE ON ss_cleanup_receipt_progress FOR EACH ROW EXECUTE FUNCTION ss_reject_immutable_evidence_update();

DO $$ DECLARE table_name TEXT; BEGIN
    FOREACH table_name IN ARRAY ARRAY['ss_cleanup_collection_work','ss_cleanup_collection_attempts','ss_cleanup_collections'] LOOP
        EXECUTE format('ALTER TABLE %I ADD COLUMN IF NOT EXISTS receipt_basis_version SMALLINT NOT NULL DEFAULT 1 CHECK(receipt_basis_version IN (1,2))',table_name);
        EXECUTE format('ALTER TABLE %I ADD COLUMN IF NOT EXISTS work_kind TEXT NOT NULL DEFAULT ''collection'' CHECK(work_kind IN (''collection'',''receipt_prepare''))',table_name);
        EXECUTE format('ALTER TABLE %I ADD COLUMN IF NOT EXISTS receipt_basis_hash TEXT',table_name);
        EXECUTE format('ALTER TABLE %I ADD COLUMN IF NOT EXISTS receipt_floor_batch_id UUID',table_name);
        EXECUTE format('ALTER TABLE %I ADD COLUMN IF NOT EXISTS receipt_floor_hash TEXT',table_name);
        EXECUTE format('ALTER TABLE %I ADD COLUMN IF NOT EXISTS receipt_head_batch_id UUID',table_name);
        EXECUTE format('ALTER TABLE %I ADD COLUMN IF NOT EXISTS receipt_head_hash TEXT',table_name);
        IF NOT EXISTS(SELECT 1 FROM pg_constraint WHERE conrelid=table_name::REGCLASS AND conname='ss_cleanup_basis_shape') THEN
            EXECUTE format('ALTER TABLE %I ADD CONSTRAINT ss_cleanup_basis_shape CHECK(
                (receipt_basis_version=1 AND work_kind=''collection'' AND receipt_basis_hash IS NULL AND receipt_floor_batch_id IS NULL AND receipt_floor_hash IS NULL AND receipt_head_batch_id IS NULL AND receipt_head_hash IS NULL)
                OR (receipt_basis_version=2 AND receipt_basis_hash IS NOT NULL AND receipt_basis_hash ~ ''^[0-9a-f]{64}$'' AND receipt_floor_batch_id IS NOT NULL AND receipt_floor_hash IS NOT NULL AND receipt_floor_hash ~ ''^[0-9a-f]{64}$'' AND receipt_head_batch_id IS NOT NULL AND receipt_head_hash IS NOT NULL AND receipt_head_hash ~ ''^[0-9a-f]{64}$''))',table_name);
            EXECUTE format('ALTER TABLE %I ADD CONSTRAINT ss_cleanup_floor_scope FOREIGN KEY(receipt_floor_batch_id,tenant_id,application_id,environment,merchant_id,authorization_generation,receipt_floor_hash) REFERENCES ss_cleanup_receipt_batches(id,tenant_id,application_id,environment,merchant_id,authorization_generation,chain_hash)',table_name);
            EXECUTE format('ALTER TABLE %I ADD CONSTRAINT ss_cleanup_head_scope FOREIGN KEY(receipt_head_batch_id,tenant_id,application_id,environment,merchant_id,authorization_generation,receipt_head_hash) REFERENCES ss_cleanup_receipt_batches(id,tenant_id,application_id,environment,merchant_id,authorization_generation,chain_hash)',table_name);
        END IF;
    END LOOP;
END $$;
ALTER TABLE ss_cleanup_collection_work ADD COLUMN IF NOT EXISTS parent_work_id UUID REFERENCES ss_cleanup_collection_work(id);
CREATE UNIQUE INDEX IF NOT EXISTS ss_cleanup_work_single_child ON ss_cleanup_collection_work(parent_work_id) WHERE parent_work_id IS NOT NULL;
ALTER TABLE ss_cleanup_current_projection ADD COLUMN IF NOT EXISTS acknowledged_batch_id UUID;
DO $$ BEGIN
    IF NOT EXISTS(SELECT 1 FROM pg_constraint WHERE conrelid='ss_cleanup_collection_work'::REGCLASS AND conname='ss_cleanup_work_version_key') THEN
        ALTER TABLE ss_cleanup_collection_work ADD CONSTRAINT ss_cleanup_work_version_key UNIQUE(id,tenant_id,application_id,environment,merchant_id,authorization_generation,config_version,receipt_hwm,manifest_hash,receipt_basis_version);
        ALTER TABLE ss_cleanup_collection_work ADD CONSTRAINT ss_cleanup_work_basis_key UNIQUE(id,receipt_basis_version,work_kind,receipt_basis_hash,receipt_floor_batch_id,receipt_floor_hash,receipt_head_batch_id,receipt_head_hash);
        ALTER TABLE ss_cleanup_collection_attempts ADD CONSTRAINT ss_cleanup_attempt_work_version FOREIGN KEY(work_id,tenant_id,application_id,environment,merchant_id,authorization_generation,config_version,receipt_hwm,manifest_hash,receipt_basis_version)
            REFERENCES ss_cleanup_collection_work(id,tenant_id,application_id,environment,merchant_id,authorization_generation,config_version,receipt_hwm,manifest_hash,receipt_basis_version);
        ALTER TABLE ss_cleanup_collection_attempts ADD CONSTRAINT ss_cleanup_attempt_work_basis FOREIGN KEY(work_id,receipt_basis_version,work_kind,receipt_basis_hash,receipt_floor_batch_id,receipt_floor_hash,receipt_head_batch_id,receipt_head_hash)
            REFERENCES ss_cleanup_collection_work(id,receipt_basis_version,work_kind,receipt_basis_hash,receipt_floor_batch_id,receipt_floor_hash,receipt_head_batch_id,receipt_head_hash);
        ALTER TABLE ss_cleanup_collection_attempts ADD CONSTRAINT ss_cleanup_attempt_version_key UNIQUE(id,receipt_basis_version);
        ALTER TABLE ss_cleanup_collection_attempts ADD CONSTRAINT ss_cleanup_attempt_basis_key UNIQUE(id,receipt_basis_version,work_kind,receipt_basis_hash,receipt_floor_batch_id,receipt_floor_hash,receipt_head_batch_id,receipt_head_hash);
        ALTER TABLE ss_cleanup_collections ADD CONSTRAINT ss_cleanup_collection_attempt_version FOREIGN KEY(attempt_id,receipt_basis_version) REFERENCES ss_cleanup_collection_attempts(id,receipt_basis_version);
        ALTER TABLE ss_cleanup_collections ADD CONSTRAINT ss_cleanup_collection_attempt_basis FOREIGN KEY(attempt_id,receipt_basis_version,work_kind,receipt_basis_hash,receipt_floor_batch_id,receipt_floor_hash,receipt_head_batch_id,receipt_head_hash)
            REFERENCES ss_cleanup_collection_attempts(id,receipt_basis_version,work_kind,receipt_basis_hash,receipt_floor_batch_id,receipt_floor_hash,receipt_head_batch_id,receipt_head_hash);
        ALTER TABLE ss_cleanup_current_projection ADD CONSTRAINT ss_cleanup_ack_batch_scope FOREIGN KEY(acknowledged_batch_id,tenant_id,application_id,environment,merchant_id,authorization_generation)
            REFERENCES ss_cleanup_receipt_batches(id,tenant_id,application_id,environment,merchant_id,authorization_generation);
    END IF;
END $$;
CREATE OR REPLACE FUNCTION ss_cleanup_work_identity_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF ROW(NEW.id,NEW.tenant_id,NEW.application_id,NEW.environment,NEW.merchant_id,NEW.authorization_generation,
           NEW.config_version,NEW.receipt_hwm,NEW.receipt_manifest,NEW.manifest_hash,NEW.trigger_kind,NEW.principal_kind,
           NEW.staff_id,NEW.session_hash,NEW.requested_role,NEW.created_at,NEW.receipt_basis_version,NEW.work_kind,
           NEW.receipt_basis_hash,NEW.receipt_floor_batch_id,NEW.receipt_floor_hash,NEW.receipt_head_batch_id,NEW.receipt_head_hash,NEW.parent_work_id)
       IS DISTINCT FROM ROW(OLD.id,OLD.tenant_id,OLD.application_id,OLD.environment,OLD.merchant_id,OLD.authorization_generation,
           OLD.config_version,OLD.receipt_hwm,OLD.receipt_manifest,OLD.manifest_hash,OLD.trigger_kind,OLD.principal_kind,
           OLD.staff_id,OLD.session_hash,OLD.requested_role,OLD.created_at,OLD.receipt_basis_version,OLD.work_kind,
           OLD.receipt_basis_hash,OLD.receipt_floor_batch_id,OLD.receipt_floor_hash,OLD.receipt_head_batch_id,OLD.receipt_head_hash,OLD.parent_work_id) THEN
        RAISE EXCEPTION 'Original cleanup work identity and receipt basis cannot change';
    END IF;
    RETURN NEW;
END $$;
CREATE OR REPLACE FUNCTION ss_cleanup_child_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE parent ss_cleanup_collection_work%ROWTYPE;
BEGIN
    IF NEW.parent_work_id IS NULL THEN RETURN NEW; END IF;
    SELECT * INTO parent FROM ss_cleanup_collection_work WHERE id=NEW.parent_work_id;
    IF parent.id IS NULL OR parent.state NOT IN ('done','blocked','superseded') OR NEW.receipt_basis_version<>2 OR
       ROW(NEW.tenant_id,NEW.application_id,NEW.environment,NEW.merchant_id,NEW.authorization_generation,NEW.config_version,
           NEW.principal_kind,NEW.staff_id,NEW.session_hash,NEW.requested_role,NEW.trigger_kind)
       IS DISTINCT FROM ROW(parent.tenant_id,parent.application_id,parent.environment,parent.merchant_id,parent.authorization_generation,parent.config_version,
           parent.principal_kind,parent.staff_id,parent.session_hash,parent.requested_role,parent.trigger_kind) THEN
        RAISE EXCEPTION 'Receipt preparation child must retain its original scope and principal';
    END IF;
    RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS ss_cleanup_child_guard ON ss_cleanup_collection_work;
CREATE TRIGGER ss_cleanup_child_guard BEFORE INSERT ON ss_cleanup_collection_work FOR EACH ROW EXECUTE FUNCTION ss_cleanup_child_guard();
DO $$ BEGIN
    IF NOT EXISTS(SELECT 1 FROM pg_constraint WHERE conrelid='ss_cleanup_collection_attempts'::REGCLASS AND conname='ss_cleanup_attempt_read_only') THEN
        ALTER TABLE ss_cleanup_collection_attempts ADD CONSTRAINT ss_cleanup_attempt_read_only CHECK(work_kind='collection');
        ALTER TABLE ss_cleanup_collections ADD CONSTRAINT ss_cleanup_collection_read_only CHECK(work_kind='collection');
    END IF;
END $$;
CREATE OR REPLACE FUNCTION ss_cleanup_projection_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE latest ss_cleanup_collections%ROWTYPE;
DECLARE acknowledged ss_cleanup_receipt_batches%ROWTYPE;
BEGIN
    IF NEW.last_complete_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM ss_cleanup_collections WHERE id=NEW.last_complete_id AND coverage_complete) THEN
        RAISE EXCEPTION 'Last complete cleanup read must reference complete coverage';
    END IF;
    IF NEW.latest_collection_id IS NOT NULL THEN
        SELECT * INTO latest FROM ss_cleanup_collections WHERE id=NEW.latest_collection_id;
        IF latest.receipt_basis_version=1 THEN NEW.acknowledged_batch_id=NULL; END IF;
    END IF;
    IF NEW.acknowledged_batch_id IS NOT NULL THEN
        SELECT * INTO acknowledged FROM ss_cleanup_receipt_batches WHERE id=NEW.acknowledged_batch_id;
        IF acknowledged.high_inclusive IS DISTINCT FROM NEW.acknowledged_hwm THEN RAISE EXCEPTION 'Read acknowledgement must match its exact receipt frontier'; END IF;
    END IF;
    IF NEW.health='complete' AND (latest.id IS NULL OR NOT latest.coverage_complete OR latest.config_version<>NEW.config_version) THEN
        RAISE EXCEPTION 'Current complete cleanup projection requires matching complete evidence';
    END IF;
    IF latest.receipt_basis_version=2 AND NEW.health IN ('complete','partial') AND
       NEW.acknowledged_batch_id IS DISTINCT FROM latest.receipt_head_batch_id THEN
        RAISE EXCEPTION 'Prepared receipts alone cannot acknowledge a provider read';
    END IF;
    RETURN NEW;
END $$;
INSERT INTO ss_schema_versions(version) VALUES (13) ON CONFLICT DO NOTHING;

-- Schema 14: account-wide credential revocation seeks only this staff member.
CREATE INDEX IF NOT EXISTS ss_sessions_staff ON ss_sessions(tenant_id,staff_id);
INSERT INTO ss_schema_versions(version) VALUES (14) ON CONFLICT DO NOTHING;
