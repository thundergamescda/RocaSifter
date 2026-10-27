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
