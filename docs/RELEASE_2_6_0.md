# Stock and Sleeve 2 6 0 local test

This is a local test release, not the hosted credit product. It includes the
price-specific variation receiving logic, a local customer-credit ledger,
manager-protected adjustments, CSV audit export, and an observation-only
30-day zero-stock cleanup eligibility journal. No Square deletions are exposed.

Credit reservations, completed-discount settlement, cancellation, refunds and
OAuth state/token handling are domain components verified with fictional data.
They are not connected to live Square events or checkout. The OAuth module
belongs on the future hosted service and is not imported into the desktop.

The full suite must run before publishing results. Logs are written under
Testing/2.6.0. Browser acceptance uses an isolated temporary database and blocks
external HTTP through the app's requests client. Two SQLite connections sharing
one database test reservation concurrency; that is not a two-computer cloud test.

Hosted release gates remain: authenticated tenant/staff sessions, managed
PostgreSQL transactions and migrations, encrypted server-side credentials,
registered HTTPS OAuth callback, signed webhook ingress and durable jobs,
merchant/location mapping, refund policy, tax approval and device/report proof.
Render with managed PostgreSQL is the recommended initial deployment target.

The supplied ZIP installs a separate Stock and Sleeve Test shortcut/profile
using local port 8876, so the existing station can keep its default port 8765.
It does not migrate or overwrite existing station data. Use only test data;
do not enter live Square credentials. Existing source shortcuts keep pointing
to the source installation. The test preview remains unsigned.
