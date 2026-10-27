# Desktop foundation preview

This milestone packages the existing Standard receiving station with Python and
its runtime dependencies. It retains Chrome as the desktop window. OAuth,
online synchronization and automatic credit checkout are subsequent milestones.

## Build

Use Windows and Python 3.12 in an isolated build environment:

```powershell
python -m venv .build-venv
.build-venv/Scripts/python.exe -m pip install -r requirements-build.txt
.build-venv/Scripts/python.exe tools/build_desktop.py
```

Distribute the entire `dist/preview/StockAndSleeve` folder, not the executable alone.
The build includes only the explicitly listed UI/edition/branding assets, imported
code and runtime libraries; it does not copy station_data, settings, receipts,
outputs or backups. This preview is unsigned and is not an installer or updater.
Do not point the source updater at the packaged application.

## Data and launch

Source launches keep data in the existing project folder. Packaged launches use
`%LOCALAPPDATA%/Roca/Station` with station_data and outputs below it. Replacing
the program folder therefore does not replace the user's receipts or vault.
There is no automatic migration of existing business data. A packaged preview
starts with a fresh profile; preserve the existing installation until a separate
stopped-station migration and rollback workflow has been tested.

For an isolated pilot, set `ROCA_DATA_HOME` to an absolute folder outside the
program resources. Use the same Windows account when migrating DPAPI credentials.
Start with StockAndSleeve.exe and stop with `StockAndSleeve.exe --stop`. The existing
station uses the same loopback port; stop it before trying an isolated preview.
Do not open the preview against business data for a packaging test.

## Readiness check

```powershell
StockAndSleeve.exe --self-check --report readiness.json
```

This mode does not start the HTTP station, create its database, read credentials,
run migrations, watch CSV folders or contact Square. It checks bundled assets,
runtime modules and preliminary folder access. The report omits local account
paths and business records. Chrome, physical labels, scanner behavior, safe
upgrade/rollback and clean-PC launch still require workstation acceptance tests.

## Next milestones

1. Signed installer and approved migration/rollback with support diagnostics.
2. Hosted service, individual staff identities and durable sync operations.
3. Merchant OAuth and location/catalog mapping with sandbox evidence.
4. Central trade-credit ledger and reservations, then gated discount checkout.
