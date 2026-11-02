# Stock & Sleeve receiving test preview

Stock & Sleeve is an installed card-store workspace being developed for independent stores, shared customer credit, and store-owned Square accounts. This branch contains a **development test milestone, not a commercial release**. Main, existing installations, and the hosted service are not upgraded by this source checkpoint.

The receiving milestone combines central physical-intake claims, fresh enrolled MFA for protected writes, an OAuth-backed catalog/inventory operation, exact retained SKUs, and recovery on another desktop. It supports a reviewed new price under an existing parent item as well as an existing variation. Catalog confirmation and inventory authorization are separate. Reading or recovering a saved operation cannot add inventory.

See [test-pilot notes](docs/RECEIVING_TEST_PILOT.md), the [source-bound evidence summary](docs/RECEIVING_TEST_EVIDENCE.json), and [commercial security/privacy gates](docs/SECURITY_PRIVACY_RELEASE_GATES.md).

## Current verification

- 690 local unit/loopback tests and 258 actual PostgreSQL tests passed, with exact case coverage, stable source, and zero skips, failures, or errors.
- Actual Chrome with two local desktop profiles, the real local authentication/API, isolated PostgreSQL, and fictional Square responses passed cross-computer receiving, concurrent authorization, and lost-response recovery.
- The actual Windows executable passed start, repeated-launch reuse, unpriced research, canceled quit, and save-and-quit checks. The real installer copied and hash-verified all 1,411 program files and created the expected shortcuts inside an isolated project test folder.
- All 18 label profiles rendered and independently decoded the exact software payload. Six legacy ZPL comparisons were byte-identical. Physical printer/scanner acceptance remains separate.
- The package contains the exact 142 reviewed notice files. All 56 shipped native libraries matched the reviewed Python runtime or wheel sources. No unresolved native-library warning was found.

These checks use fictional customers, credit, and provider responses. They do not establish live Square checkout/reporting, physical hardware, clean-machine installation, tax classification, universal usability, or commercial readiness. Focused checks overlap the complete suites and are not additive counts.

## Running the test desktop

Use the separately supplied **Receiving Test Pilot ZIP**, extract the entire archive, read `Test results/RECEIVING_PILOT_README.md`, and run `Install Shared Pilot.cmd`. The installer creates a separate program folder, pilot profile, port, and Desktop/Start Menu shortcuts. Occupied shortcut names receive a suffix. It does not migrate business data or start the app.

Open the new **Stock & Sleeve Pilot** shortcut to start the service and app window together. Chrome is required; the compiled package includes Python. Use fictional data and Quit inside the app when finished. Preserve pending journals until their outcomes are resolved.

Hosted receiving needs the matching reviewed schema-6 server, configured Square OAuth, current staff permission, fresh enrolled MFA, and explicit receiving enablement. Installing a desktop package does not deploy that server. This checkpoint has not been deployed to the existing hosted service.

## Feature scope

The workspace includes scanner/CSV and manual card/slab intake, card review, labels, price research, and trade/customer-credit interfaces. The supported-game scope includes Pokémon, Magic, Riftbound, One Piece, Union Arena, Gundam, sports, and additional scanner-supported games. Device compatibility and pricing-source coverage require their own acceptance.

Central claims bind the original physical delivery across desktops. New hosted cash/credit acceptance consumes an eligible claim once. Claimed batches block local-wallet and local-Square fallbacks. Exact accepted results and uncertain requests remain recoverable through their original identities.

The hosted owner export uses version 4 classifications across 26 collections. It exposes accepted customer-bound evidence, withholds other customers and competing drafts, and excludes raw provider request bodies and secrets. Export is not deletion or account closure.

Legacy local-profile history and direct-token configuration remain in the inherited app for older, unclaimed workflows. This checkpoint does not represent their removal or a complete transition of every path to OAuth. New tracked receiving uses the hosted OAuth operation. Review these distinctions before adopting a pilot.

## Credit and commercial release

**Production automatic credit redemption through a tracked Square discount is not accepted or enabled by this milestone.** Shared ledgers, reservations, settlement, retries, and reversal logic have isolated tests; native Square checkout, order linkage, reporting, refund behavior, and device operation still need supported end-to-end acceptance. Do not use the pilot for production redemption.

Old-SKU cleanup is conservative review/dry run. This checkpoint does not delete old Square SKUs. New parent-item creation, full independent-store account lifecycle, delivery/recovery/support, reconciliation operations, and commercial sales-channel integrations remain engineering work.

Commercial pricing rights, Idaho treatment of deferred credit, privacy/retention/deletion policies, asset/contributor rights, production restore, signed distribution, actual two-computer use, physical hardware, and Marketplace approval remain release gates. There is no collection for data sale.

Independent-store owner invitations are under development in a separate candidate and are not part of this receiving checkpoint.

## Building and testing the source

The reviewed Windows x64 environment uses Python **3.12.16**. The [reviewed Windows build/test version list](requirements-reviewed-windows-build.txt) records its 46 installed package versions; test-only and hosted libraries in that environment are not all shipped in the desktop. The notice manifest binds the shipping package versions and exact legal texts. The broad development requirement files alone do not reproduce the recorded environment.

Create a fresh Python 3.12.16 virtual environment and install the appropriate requirements together with the reviewed Windows version list. Then run:

```text
python tools/test_release.py
python tools/test_hosted_postgres.py
python tools/build_desktop.py --output-directory dist/fresh-receiving-test
```

The PostgreSQL runner requires a privately supplied disposable database connection. Its tests use isolated fixture namespaces; never point development acceptance at a merchant database. Browser scripts require Chrome and their separate fixture configuration. An existing compiled output directory is refused.

The desktop compiler uses only the selected interpreter/runtime and Windows directories for its child PATH, removes inherited Python/frozen-process overrides, and rejects PyInstaller's unresolved-native-library warning even after exit zero. Optional Python-module warnings remain separately reviewable. Inspect final native origins, assets, notices, dependency results, and actual executable behavior before delivering any new package.

Use [hosted receiving](docs/HOSTED_RECEIVING.md), [physical intake](docs/PHYSICAL_INTAKE_CLAIMS.md), [trade bridge](docs/SHARED_TRADE_BRIDGE.md), [pricing research](docs/PRICING_RESEARCH.md), [customer export](docs/HOSTED_CUSTOMER_EXPORT.md), and [privacy version 4](docs/PRIVACY_RECEIVING_EXPORT_V4.md) for the specific contracts. Earlier operator manuals describe inherited workflows and are not current release acceptance.
