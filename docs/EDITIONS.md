# Choose your edition — 2.5.0

Both editions include export pickup, manual items/slabs, the cash/store-credit offer calculator, approved-trade summaries, price-specific Square variations, Price research, Pokémon $1 labels, saved retries and the existing label layouts.

## Standard

No credit tracker module, customer-credit pages, balances or credit tables. The existing trade-in calculator still offers cash/store-credit estimates and records the agreed amount. Payment/credit delivery is handled separately. Credit API requests are disabled. Existing credit data, if any, is retained untouched when switching editions.

## Credit tracker

Adds local customers, balances, approved-trade credits, credit used against a sale reference, admin-protected opening/correction entries, full CSV history export and an audit trail. Retries cannot grant an approved trade twice; balances cannot go negative. Opening balances are only allowed before the first entry.

1. Approve a store-credit trade using the normal trade workflow.
2. Choose **Track this approved credit**, then choose/add the local customer.
3. Choose **Add approved trade** and record the agreement. The app reads the approved amount itself.
4. At checkout, verify the local balance, complete the sale in Square using the store’s approved process, then **Record credit used** with the sale reference.
5. Use **Opening balance / adjustment** with Admin authentication for a correction. Enter a clear reference and reason. **Export history** downloads the complete ledger; the screen displays the latest 100 entries.

This is tracking only: **no Square gift-card loads, payments, orders, discounts, customer balance updates or tax/reporting writes**. It cannot promise a specific gross/net/tax result for a transaction staff enter in Square. A discount and a payment have different reporting/tax effects; confirm your chosen Square workflow with your bookkeeper. Credit is not automatically redeemed at POS.

Balances are local to this installation, not shared across laptops or synced with Square. A Square customer reference is only a reference. One store should use one designated credit station until a shared ledger exists. Protect and back up the full station_data directory while Roca is stopped.

## Install or update

For an older installation, the versioned **Standard Auto Update ZIP** includes
the app and its updater. Extract all its files into a separate folder, run
**Install Roca Update.cmd**, choose your existing Roca folder, and click
**Update & reopen Roca**. It creates a backup, installs the included version and
dependencies, and reopens Chrome. GitHub access is not needed for this supplied
release; downloading Python dependencies may need internet. Keep application.zip
zipped beside the updater. This package selects Standard while retaining any
existing credit history on disk. The regular updater continues to preserve the
installed edition for future GitHub updates.

1. Stop the existing Roca instance with **Stop Roca** and back up station_data and outputs.
2. Extract the chosen edition ZIP. For a fresh PC, keep all its files together, run **Install dependencies.cmd**, then **Start Roca.vbs**.
3. For an existing station, copy the new application files into its existing folder. Preserve station_data, outputs, backups and settings. The ZIP contains no user data to overwrite them. Rerun **Install dependencies.cmd**, then start Roca and verify **2.5.0**.
4. The included **roca_edition.json** selects the edition. Copy the chosen package’s file when intentionally switching editions. The automatic updater preserves that selection.

Do not run the GitHub automatic updater to obtain this local preview until these changes are published: it currently targets main, which still contains 2.3.1. A direct ZIP here is a local build, not an installed update or a GitHub publication. It needs Python and Chrome; it is not a standalone executable.
