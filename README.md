# Roca Station 2

A local receiving workspace for Roca Sifter exports, Square inventory, trade-in offers, and Zebra 2″ × 1″ labels.

![Roca Station trade-in workspace](docs/screenshots/workspace.png)

## Start on Windows

1. Download **Code → Download ZIP** from GitHub and extract the complete ZIP into a permanent, writable folder. Keep every application file together; this is no longer a single-file app.
2. Install Python 3.10 or newer if needed.
3. Run **Install dependencies.cmd** once, including when upgrading from version 1.
4. Run **Start Roca.cmd**. The launcher opens Google Chrome and displays **2.0.0**. If an older station owns the port, close its Python/console window first. Edge is never launched as a fallback.
5. Open **Admin settings**, create your own password (at least 12 characters), and enter your Square token. There is no default password.
6. Choose Production for a real store or Sandbox for a sandbox token. Test the connection, select the receiving store, and save.
7. Import a Roca CSV or configure automatic export pickup in Admin. Review every included card before receiving stock or printing labels.

Python command-line equivalent:

```sh
python -m pip install -r requirements.txt
python roca_to_square.py
```

The app serves only the local computer at `127.0.0.1`. A responsive layout supports smaller browser windows, but this release does not expose a network server for other devices.

## Four workflows, one receipt

| Workflow | What it does | Inventory behavior |
| --- | --- | --- |
| **Bulk scan / sift** | Imports all bins, applies a value threshold, and identifies what deserves further processing | Never receives stock. Move the same receipt to the next workflow. |
| **Walk-in trade-ins** | Reviews card details and calculates adjustable cash/store-credit offers; records the agreed buy and a printable acceptance record | Acceptance is recorded before receiving or retail labeling. Payment/credit issuance happens outside this app. |
| **Price & label** | Reviews final prices and produces side-fold QR, standard, or top-fold labels | Choose **New incoming copies** to receive stock, or **Already in inventory** for labels without adding copies. |
| **Online preparation** | Prepares a listing review CSV and a storage ticket for an online bin or a display case | **Receive & prepare** receives new stock first. **Prepare only** creates files without changing stock. Nothing is published online. |

Switching navigation tabs opens that workflow. Use **Move receipt to…** to continue the same physical receipt in another workflow. Moving it does not receive it again. A received receipt is safe to reopen and reprint.

**Activity & orders** records receipt history and manual online-order pull tasks. A task includes an order reference, card details, quantity, and physical storage location. Staff explicitly confirm removal from the display or bin. Task confirmation does not change stock; live marketplace sale synchronization is not connected.

## Reviewing cards

- Open a card’s editor to correct name, set, collector number, language, condition, finish/treatment, quantity, price, or storage location.
- Finish is a free-text field with optional suggestions, not a fixed list. Future/custom treatments are supported.
- Changing identity or treatment clears the review confirmation and a legacy SKU override in the editor. Confirm the price for the corrected variant; no live market-price lookup is connected.
- **Split one copy** separates one card from a quantity so it can receive a different condition or treatment. The total incoming quantity is preserved by the UI.
- Confirm each card, or explicitly confirm that all included cards have been physically reviewed.
- Search and review filters operate across the entire receipt. The table renders 40 rows per page; an 11,000-row import was exercised in the browser.
- After accepting a buy or receiving stock, card identity/quantity/price are locked. Notes and storage assignments can still be updated. Handle post-receiving stock corrections in Square and record a separate correction workflow rather than rewriting a committed receipt.

Cash and store-credit percentages are configurable defaults, not recommended buying rates. Offers use included quantities and proposed retail prices. Accepted buys are saved separately from inventory receiving, and include an acceptance PDF. They do not transfer money, issue store credit, or create a legal customer signature.

## Password-protected Admin

![Locked admin settings](docs/screenshots/admin.png)

Admin protects the Square credential, environment/location, watched folder, and workflow defaults. Operators can use the saved connection without entering or seeing the token. A blank token field keeps the saved credential. Saving a changed Square connection validates its active location before accepting it.

Passwords are stored as salted PBKDF2 hashes; sessions use HttpOnly/SameSite cookies, expire, and are revoked on password change. Closing the Admin dialog locks it. Login attempts are rate limited. The old unprotected settings/write routes are retired, and public settings never contain the token or password hash.

Credentials are encrypted locally: Windows uses the current Windows user’s DPAPI; other supported development hosts use a local Fernet key with restricted files. This protects the application boundary and local credential files; it is not an operating-system account isolation system. Keep the workstation account and `station_data` private. There is no browser password-reset bypass.

An old `roca_settings.json` token is migrated into the vault on startup and removed from that settings file only after the encrypted credential is readable. Existing backup copies of older settings are outside this migration.

## Automatic Roca export pickup

In Admin, enter an existing export folder on the station computer and choose its default workflow. Leave it blank for manual imports.

The watcher checks CSVs for unchanged size/mtime before reading and checks again after reading. Imported files become **unreviewed drafts**; they never accept buys, print, or receive stock automatically. An invalid file is reported without preventing valid files from being picked up. Saving a corrected file allows it to be examined again.

Identical CSV content, including a renamed copy, opens its existing receipt. Manual intake offers a deliberate **new delivery** override for genuinely additional physical stock. Filename changes alone do not make a new delivery. Corrected or cumulative exports may have different content and cannot be reliably identified as the same physical receipt without stable Roca scan identifiers. Review them for overlap before approval; do not blindly receive re-exports. The watcher does not modify Roca files or control Roca’s mechanical bins.

## Square receiving and duplicate cards

New card identifiers are independent of CSV row order and price. They include card name, collector number, condition, finish, language and set. Supply consistent set data to keep editions separate. Common condition/non-foil aliases are normalized; different variants stay separate.

Repeated matching CSV rows are grouped for the inventory request. A matching Square SKU receives an additive adjustment from `NONE` to `IN_STOCK`, not an absolute count: 4 existing + 2 incoming = 6, absent intervening sales. Existing prices are preserved, and a mismatch between the CSV label price and Square price stops preflight. Multiple Square variations sharing a SKU also stop preflight.

Each receipt has one receiving operation. Repeating **Receive** after success is a no-op. The local SQLite journal stores outgoing payloads and Square idempotency keys before sending; retries use the saved payload, including timestamps, and reuse confirmed responses. This covers tested restarts and lost-response cases. A preflight error before any catalog/inventory write leaves the receipt editable; ambiguous or partial writes lock it for a saved retry. Restore the original environment/location before retrying. Do not create a new receipt as a workaround. Very old pending requests, manual Square changes during a pending operation, or persistent Square errors require merchant-side reconciliation; do not assume stock is correct until Square confirms it.

Square catalog and inventory are separate API operations. The app reports partial failures. When the item variation is sold through Square POS at the receiving location, Square’s inventory behavior applies; a custom-amount sale cannot identify the card. Validate your real POS/location/variation-tracking configuration before rolling out shared stock.

**Version 1 stock:** older releases generated row-dependent SKUs. Add the existing **12-digit Square SKU** in a `Square SKU` or `SKU` CSV column to preserve that original item/label and add copies to it. Do not silently remap existing items by card name. No migration of live Square items was performed.

## Zebra labels

Side fold is the default: card name, set/number, condition, finish, and price on the left; QR code on the right. Fold vertically down the center of the 2-inch width, with the QR half flat on the sleeve’s back. Apply adhesive to a sleeve/toploader, not a collectible card. Long text is shortened to fit the physical label.

- Stock is **2 inches wide × 1 inch high**; print PDFs at **Actual size / 100%**, with no fit-to-page scaling.
- Choose the actual ZD411D resolution: 203 or 300 DPI.
- QR contains the same 12-digit SKU used in Square. It requires a QR-capable scanner and a tested Square lookup workflow; a 1D laser scanner cannot read it.
- Standard and top-fold layouts retain linear Code 128.
- PDFs and raw ZPL are written to `outputs/` with unique names and linked in the receipt. Reprinting does not receive inventory.
- ZPL is a download for a configured raw-print workflow; this release does not submit jobs directly to a USB/wireless printer. The printer stays connected by USB unless separately configured with supported networking.

PDF barcode/QR decoding was tested at both supported resolutions. Actual Zebra output, media calibration, scanner compatibility, and a real Square sale still need a store acceptance test.

## Online preparation boundaries

The CSV is a **generic listing review sheet**, with SKU, identity, variant, quantity, price, channel and storage location. It is not guaranteed to match TCGplayer/eBay’s native import schema. Listings must be published through a verified seller workflow. Marketplace APIs, automatic delisting, live price feeds, and automatic order notifications are not implemented in this release. Physical bins are configured in Roca; this app records where the approved cards are stored.

## Upgrading, files and backups

Close the old station before updating. Copy your old `roca_settings.json` into the new folder before the first version-2 launch if you want its token migrated. For subsequent version-2 updates, preserve the entire **station_data/** directory and **outputs/** while the station is closed. Do not replace them with empty directories from a clean installation. A Windows credential must be opened by the same Windows user; otherwise save a replacement token through Admin.

`station_data/` holds receipts, operations, preferences, password hashes, and encrypted credentials. It is excluded from Git, along with old settings, Python caches and generated outputs. Back up the complete directory while the app is closed, keep backups private, and restore to the same workstation/user or re-enter the token. Deleting it loses receipt history and duplicate protection.

## Validation and development

```sh
python -m pip install -r requirements-dev.txt
python -m unittest discover -v
python -m playwright install chromium
python browser_smoke.py
```

The browser test uses an isolated temporary database, fictional receipt data, and no live merchant credentials. It exercises trade-in editing, arbitrary finish text, review/acceptance, labels, tab transfer, display-case preparation, downloads, admin setup/save/lock, pull tasks, mobile layout, and 11,000-row intake/paging/search. Screenshots are written to a temporary folder. `ROCA_BROWSER_PATH` can select a local Chrome/Chromium executable.

The automated backend suite covers auth boundaries, token redaction/encryption/migration, receiving deduplication, saved retries across restart, duplicate cards, trade approval, folder pickup, listing sheets, and label decoding. Square interactions are mocked. Browser and API validation here do not establish Windows DPAPI behavior, physical printing, or a live merchant integration on your store machine.
