# Hosted service pilot

This is a private-pilot backend, not a commercial-release claim. It uses
PostgreSQL transactions, tenant-scoped queries, individual staff accounts,
revocable sessions and encrypted Square OAuth credentials. Live checkout stays
disabled until the tax/device/report gates pass.

## Render web service

Region Oregon, paid Starter web compute. Existing database is
stock-and-sleeve-db in the same region; its public inbound access is blocked.
Use the private database connection string in DATABASE_URL, never in source.

Build: `pip install -r requirements-hosted.txt`

Pre-deploy: `python -m hosted.manage migrate && python -m hosted.integration`

Start: `gunicorn hosted.wsgi:app --bind 0.0.0.0:$PORT --workers 2 --threads 2 --timeout 60 --access-logfile - --access-logformat '%(m)s %(U)s %(s)s'`

Health path: `/readyz`. `/healthz` reports only process liveness. `/readyz` reports database/service readiness
and explicitly does not claim commercial readiness. Public base URL is inferred
from RENDER_EXTERNAL_URL or configured through PUBLIC_BASE_URL.

Server configuration:

- DATABASE_URL: existing private PostgreSQL URL; TLS is required by the client.
- APP_ENCRYPTION_KEYS: one or more comma-separated high-entropy server secrets,
  at least 32 characters each, newest first. A Fernet key is accepted directly;
  other hosting-generated random secrets use domain-separated SHA-256 derivation.
- BOOTSTRAP_TOKEN: one-use pilot-owner onboarding authorization. Without it,
  public owner creation is disabled. Do not put it in a URL, log or repository.
- SQUARE_APPLICATION_ID and SQUARE_APPLICATION_SECRET: server-only OAuth app
  configuration. SQUARE_ENVIRONMENT defaults to sandbox. Do not configure
  production checkout before acceptance.
- SQUARE_WEBHOOK_SIGNATURE_KEY: signature key for the exact endpoint
  PUBLIC_BASE_URL/webhooks/square.

Register PUBLIC_BASE_URL/oauth/square/callback in Square's Developer Console.
Only the signed-in owner can begin connection. The current initial scope set is
read-only; checkout write permissions require a later reviewed consent update.

## Test evidence

Local HTTP/security checks: `python -m unittest -v test_hosted_boundaries`.
Real PostgreSQL acceptance: `python -m hosted.integration` from a host on the
private network. It creates and drops only an isolated ss_test_<uuid> schema,
uses fictional accounts and makes no Square requests. Save the run output as
deployment evidence. A skipped local database test is not a passing cloud test.

## Remaining release work

MFA, staff recovery, email verification/delivery, audited owner transfer,
processor agreements and retention/deletion operations remain release gates.
The event endpoint validates signatures and stores minimal durable jobs; a
reconciliation worker and automatic checkout integration are not yet deployed.
RLS/role hardening and tenant escape tests must supplement application scoping.
No analytics trackers, data-sale pipeline or AI training on merchant data exist.
