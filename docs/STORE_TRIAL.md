# Store trial for Roca Station 2

These are the checks that still require the actual store equipment/account. Automated tests use an isolated local database and mocked Square responses.

1. Close any older Roca Station, install the complete v2 folder and dependencies, and double-click Start Roca.vbs. Verify 2.0.2 in Chrome with no command window. Close Chrome and start again: it should reopen the same station. Save, close Chrome and use Stop Roca.vbs; confirm it reports stopped. Start again and verify saved receipts remain.
2. Create an admin password, enter a sandbox token, test locations and save. Close Admin. Verify it requires the password again and the operator screens never display the token. Restart the app and verify settings persist.
3. Export a small Roca batch with duplicate copies and at least two finishes. Open the draft, split one copy, correct its finish/condition/price, and confirm review.
4. Generate side-fold labels only. Verify 2″ × 1″ physical dimensions, set/number/condition/finish/price on the left, code on the right, and a flat code after folding on a sleeve. Use the actual printer DPI and Actual size/100% printing.
5. Scan one code into the actual Square register with the shop’s scanner. Confirm it selects the correct variation; PDF software decoding alone does not prove this.
6. Receive that test receipt in Sandbox. Compare added quantities and variation tracking. Repeat Receive on the same receipt and confirm it adds nothing. Move the receipt between workflows and reprint; confirm stock stays unchanged.
7. Exercise an accepted trade-in, view its acceptance record, and verify no payment/credit is issued by the app. Verify an unaccepted trade-in cannot receive stock or retail labels.
8. Prepare a display-case listing batch and a dedicated-bin batch. Check location fields on the CSV/ticket. These files are review aids, not automatic marketplace listings.
9. Test folder pickup with a newly exported CSV. Verify it remains an unreviewed draft, then copy/rename the identical file and verify it opens the same receipt. Review edited/cumulative exports for overlap manually.
10. Once Sandbox behavior is confirmed, configure the real Production store. Start with a small controlled receipt and sale, checking stock at the actual location and using the catalog variation rather than a custom amount. Direct Zebra printing and marketplace synchronization are separate work.

Keep station_data and outputs with the installation. Back up while the station is closed. Do not wipe receipt history or create a new receipt to bypass a pending Square operation.
