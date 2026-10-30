# Shared Pilot packaging and installation

The Shared Pilot supports invited online customer-credit accounts/history and
manager corrections alongside reviewed intake, pricing research and labels.
Square OAuth requires secure sandbox app configuration and merchant consent.
Hosted accepted trades and tracked-discount checkout require separate final
server, sandbox, device, tax and reporting acceptance. A successful package test
is not consumer certification or proof of live Square behavior.

## Build from a stable source snapshot

On Windows, after the required regression/browser evidence is current:

```powershell
.build-venv/Scripts/python.exe tools/build_desktop.py --output-directory dist/shared-pilot-2.6.0-verified-r2
```

Choose a fresh output folder for each build. The builder refuses an existing program folder.

The builder includes `desktop_build.json` with `format_version: 1`,
`channel: private-pilot`, a build ID and source-input hashes. It fails if program
inputs change while PyInstaller runs. The packager independently checks those
inputs against the selected source and compares raw bundled UI/branding assets
to their recorded hashes. It does not prove that an arbitrary executable was
compiled from those sources; trusted build provenance and signing remain gates.

## Create the ZIP explicitly

```powershell
.build-venv/Scripts/python.exe tools/package_preview.py --program-directory dist/shared-pilot-2.6.0-verified-r2/StockAndSleeve --output Downloads/Stock-and-Sleeve-2.6.0-Shared-Pilot.zip --evidence-directory Testing/2.6.0 --evidence automated-tests.log --evidence automated-summary.json
```

No program/output folder is hardcoded and no evidence is included by default.
Repeat `--evidence` only for exact reviewed filenames; `--help` lists the
allowlist. Missing, duplicate, unlisted or nested evidence selections fail.
Screenshots and historical logs need deliberate review for fixture scope,
credentials, customer data and paths before selection. The import mapping
failure screenshot is intentionally excluded. Do not claim all selected logs
describe the final source merely because their names appear in the allowlist.

The tool rejects credential/data paths at any depth, Windows path aliases,
unknown JSON data, unexpected top-level content and links/junctions. It hashes
the complete selected program before and after copying, rechecks source/evidence
stability, verifies the ZIP contents and writes a separate SHA256 download
checksum. Existing output/checksum files are not overwritten. Choose a new
artifact name for a new attempt. These hashes detect changes against the
supplied record; they do not authenticate the publisher like a signature.

Two exact cryptography50.0.2 vendor SBOM paths are allowed alongside the app's
own JSON assets. Packaging compares their copied bytes with the installed
pinned distribution and verifies that installation's SHA256/size RECORD entries.
Other JSON files, metadata versions or paths remain rejected. The installer
checks the same exact path exceptions and their package hashes; it does not
require a Python installation or query a package service on the user's PC.

## User installation

Extract the whole ZIP and run **Install Shared Pilot.cmd**. It verifies program
hashes before and after copying; no app starts automatically. It creates:

- Program files under `%LOCALAPPDATA%/Programs/StockAndSleevePilot/<unique tag>`.
- A new profile under `%LOCALAPPDATA%/StockAndSleeve/Pilots/<unique tag>`.
- An available loopback port reserved for that installation and persisted in
  `pilot-install.json`; both launchers set the same profile/port explicitly.
- Desktop/Start Menu **Stock & Sleeve Pilot** and **Stop Stock & Sleeve Pilot**
  shortcuts using the approved icon. Occupied names receive a unique suffix;
  existing main, Test and other Pilot links keep their targets and contents.

Use the Pilot shortcut. Quit inside the app or its matching Stop Pilot shortcut
finishes active work and stops that profile. Closing the window alone can leave
the station running. Port availability is checked at installation; another
application can occupy it later. Resolve that conflict without switching the
pilot to another store's profile. No existing store is stopped, migrated,
updated, copied or automatically launched by installation.

If someone directly opens the frozen EXE without `ROCA_DATA_HOME`, a valid
private-pilot manifest selects
`%LOCALAPPDATA%/StockAndSleeve/PilotDirect/<build_id>` and default port 8877.
It never defaults to the legacy `Roca/Station` profile. An explicit environment
override still wins; do not reuse a business profile for a pilot. Missing,
invalid or other-channel build metadata fails before opening a profile and
shows a branded recovery message. Source-project launches retain their existing
data/port behavior. Separate direct builds may contend for port 8877; the
installed shortcuts provide each installation's own persisted port.

Chrome is required; Python is bundled. This is an unsigned pilot. Do not point
the source updater at packaged files or use it for production credit redemption.
Local database/output files are not fully application-encrypted. Removing a
program folder or shortcut is not a customer-rights deletion process; preserve
uncertain financial journals until a verified recovery resolves their outcome.

## Evidence from isolated regressions

`test_package_preview.py` uses fictional bytes, never executes its fake EXE,
and installs test files/shortcuts only inside temporary directories. It covers
stale/tampered build identity and raw assets; copy races and changed ZIPs;
path escapes, Windows aliases, nested merchant/credential payloads and junctions;
explicit evidence selection; pre/post-copy installer validation; existing
artifact/shortcut preservation; and unique profiles/ports with paired launchers.
`test_station_paths.py` covers direct-EXE isolation, invalid manifests, explicit
overrides and branded failure before application initialization. Real clean-PC,
browser/device and final packaged acceptance remain separate checks.
