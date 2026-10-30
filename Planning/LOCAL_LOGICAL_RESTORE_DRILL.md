# Disposable local logical backup/restore drill

The controlled disposable fixture wrapper executed this drill on 2026-10-28;
the exact run and its limits are recorded below. Future runs still require
fixture-owner review of `tools/pg_logical_restore_drill.py`. Twelve pure mocked
guard tests passed in `tools/test_pg_restore_drill_guards.py`; they invoke no actual
PostgreSQL process or connection. The original non-executable preparation copy
is retained in Planning as historical preparation; the staged tools file is
authoritative. The drill imports no desktop application launcher.

This is a bounded logical restore check using fictional accounts/cards/balances.
It is not Render PITR, a production backup policy, or RPO/RTO evidence. It neither
reads nor migrates an existing app schema and makes no Square/network-provider
request. It creates its own source and restored databases from `template0`.

## Verified local fixture run

Run `4c84952645134792a3eedbf10cef1003` passed from 22:52:22 to 22:52:29 UTC on
2026-10-28. Evidence is in
`Testing/next-security/restore-drills/4c84952645134792a3eedbf10cef1003/restore-drill.json`
and the adjacent `restore-drill.log`. Selected exact rows and sequence states
matched after restore; source rows stayed unchanged; credit/lot conservation,
accepted-trade replay, checkout denial, same-key MFA decrypt, wrong-key denial,
TOTP/recovery continuity and disabled-session guards passed. Both exact owned
fixture databases were removed after their OIDs and ownership markers matched.
Hosted source/tool fingerprints remained stable throughout that run.

The earlier failed pre-creation run `3ccc96e78ab54b45b6191462b56e5446` is preserved
in the same evidence parent. PostgreSQL rendered its `inet` address with `/32`;
the corrected fact query uses `host(inet_server_addr())` before applying the
existing loopback check. Its mocked regression is included in the twelve guards.
This fictional local logical restore does not test Render PITR, production key
custody, outage recovery, retention, or production RPO/RTO.

## Required private wrapper configuration

The wrapper must set the following environment values without displaying them:

| Environment value | Required meaning |
| --- | --- |
| `SS_DISPOSABLE_PG_FIXTURE` | Exactly `1`, explicitly marking this local disposable fixture |
| `DATABASE_URL` | Fixture connection only: host exactly `127.0.0.1`, role exactly `ss_fixture`, maintenance database exactly `postgres`, password supplied, TLS required |
| `SS_PG_BIN_DIR` | Absolute independently verified PostgreSQL 18.6 binary directory |
| `SS_PG_BIN_SHA256_JSON` | Exact JSON map of `pg_dump.exe` and `pg_restore.exe` to independently verified SHA256 hashes; omit `.exe` on non-Windows systems |
| `SS_RESTORE_MFA_KEY` | A freshly generated disposable Fernet key, unrelated to any live deployment key; retain only privately for this run |
| `SS_PG_EXPECT_VERSION` | Optional, defaults to `18.6`; server and both pinned binaries must match |

Clear inherited `PGSERVICE`, `PGSERVICEFILE` and `PGOPTIONS`. No shell command or
argument should contain a connection URL, username, password, seed, TOTP or
recovery code. Do not reuse a live MFA encryption key. The tool takes routing and
credentials from environment only. Its child process environment is restricted
to OS necessities and the exact fixture `PG*` parameters; unrelated app keys are
not passed to the PostgreSQL tools.
An optional connection-URL `connect_timeout` of 1–10 whole seconds is accepted;
the drill itself uses a fixed ten-second connection timeout.

The fixture role needs `CREATEDB` on this local disposable cluster. The drill
checks that privilege but never grants it or alters roles. If unavailable, stop
and let the fixture owner decide whether to grant it in this cluster. The tool
does not fall back to an application database or a privileged production role.

## Operator sequence

1. Verify the binary directory/hashes against the fixture setup evidence and
   confirm the fixture is local, disposable, TLS enabled and otherwise idle.
2. Supply the private environment through the root's existing credential wrapper.
   Do not print `$env:DATABASE_URL` or shell history containing credentials.
3. After source/tool review and test freeze, invoke the isolated Python runtime
   with `tools/pg_logical_restore_drill.py --execute-local-disposable`, from the
   next-security workspace. Both the flag and environment marker are required.
   The preflight-only invocation is
   `python -m unittest -v tools.test_pg_restore_drill_guards`; every database and
   binary execution in those tests is mocked or unreachable.
4. Read `Testing/next-security/restore-drills/<run UUID>/restore-drill.json` and
   `restore-drill.log`. A process exit of zero is necessary; verify `passed`,
   `source_snapshot_stable`, exact initial row/sequence equality, source unchanged,
   security/business checks, archive hash and every cleanup result too.
5. Treat any failure as a failed drill. Preserve its redacted logs/manifest and
   investigate in the local fixture. Never rerun against a live service or fix
   a failure by changing the database URL to a production database.

## Exact drill scope

The program generates distinct names `ss_test_<uuid>` for the source database,
restored database and application schema. No caller can supply those names or
an archive. It seeds fictional owner/manager/customer data, disables the manager,
enrolls the fictional owner in MFA, records a 5,000-cent opening balance and an
explicitly accepted 1,000-cent trade, and keeps checkout/policy approval disabled.
Plain fixture credentials exist only in process memory; the dedicated encryption
key comes from the private wrapper environment.

It dumps only its fresh schema to a custom archive with the pinned `pg_dump`,
then uses the pinned `pg_restore` to restore into the separate fresh database in
one transaction, with no owner/privilege restoration. The restore command uses
the nonsecret connection-string parameter
`--dbname=application_name=ss_disposable_restore` to select direct connection
mode; host, database, user and password remain in environment. PostgreSQL's
`pg_restore` requires an explicit direct-connection option and supports a
connection string for `--dbname`; libpq supplies defaults from environment.
See the official [pg_restore](https://www.postgresql.org/docs/18/app-pgrestore.html)
and [libpq environment](https://www.postgresql.org/docs/18/libpq-envars.html)
documentation. Creating from `template0` avoids fixture-specific additions in
`template1`.

Before functional probes, selected exact application rows and sequence states
must match the source in memory; logs contain only row counts and hashes, not
raw rows. It then checks ledger/lot conservation, exact accepted-trade and
opening-entry idempotency, immutable trade evidence, disabled checkout, MFA seed
decrypt with the same key, wrong-key denial, restored TOTP verification, one-use
recovery, disabled-session denial and the restored database session guard.
It checks source rows again after all target probes: the source data must remain
unchanged. Functional MFA writes happen only in the restored fixture database.

The archive and logs contain fictional data only. Dumps never contain the MFA
encryption key, and the tool checks for plaintext fixture secrets. A real backup
policy would separately protect and test key custody/rotation; this drill does
not establish that policy or production key recovery.

## Cleanup and interrupted runs

`owned-namespaces.json` records only planned/created names, database OIDs and a
run-specific ownership comment. Cleanup considers only names created by this
run, rechecks the exact OID/comment, and drops those exact databases after closing
its connections. It never enumerates a wildcard and drops everything matching
`ss_test_*`, uses `DROP ... CASCADE` on other schemas, or terminates unrelated
sessions. A blocked/changed ownership check leaves the database for owner review.

If the process is killed before `finally` runs, inspect that manifest privately.
The fixture owner may remove only the two exact recorded databases after
independently matching names/OIDs/comments. A missing ownership comment is not
permission to broaden cleanup; investigate before deletion. Keep the archive and
redacted evidence until the fixture review is complete. No local cleanup result
implies anything about retention/deletion of real customer or Render backups.
