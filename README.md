# Roca Sifter → Square and 2 × 1 inch labels

The updated `roca_to_square.py` adds the CSV quantity to Square inventory after creating catalog variations. It uses the real variation IDs returned by Square, sets `IN_STOCK` at the receiving location, and enables inventory tracking. Selling that catalog variation through Square POS at the same location lets Square reduce its stock. A custom amount sale does not identify the variation and cannot reduce that card's inventory.

## Install and run

Requires Python 3.9+:

```sh
python -m pip install requests reportlab
python roca_to_square.py --csv cards.csv --dry-run
python roca_to_square.py --csv cards.csv --dry-run --label-layout fold --dpi 203
```

Print PDFs at **Actual size / 100%**, on 2 inch wide × 1 inch high stock, without fit-to-page scaling. Alternatively send the generated ZPL as raw data to a compatible Zebra printer. Your Zebra ZD411D is a direct thermal printer available in 203 or 300 DPI versions. The default is 203 DPI; check the printer configuration label and select `--dpi 300` for a 300 DPI unit. Use direct thermal label stock. Physical scan reliability also depends on printer calibration, darkness/speed, media and the scanner; test one printed label before printing a batch.

The standard label has the card name, price, then barcode. Long names are shortened to fit. Labels use a 12-digit Code 128 value as the Square SKU, with wide narrow bars and blank quiet zones. The old longer barcode was difficult to fit. This changes barcode values for new imports: **do not use these new labels for previously uploaded items without reconciling their Square SKUs**. The hash conversion is deterministic, and duplicate resulting SKUs are rejected within an upload and checked against Square.

The optional `fold` layout folds across the middle of the 1 inch height, producing two 2 inch × 0.5 inch faces. Place the barcode half flat on the back of a sleeve/toploader; fold the name/price half over its top edge to face the front. The front text is rotated so it reads upright after folding. Keep the barcode and its blank margins flat, away from the edge. Apply labels to protective sleeves, not directly to collectible cards. Thick holders consume fold space; printer model and holder dimensions are needed for final physical tuning. No fold guide is printed through the barcode.

## Square upload

Set `SQUARE_ACCESS_TOKEN` securely in your own machine's environment. The token needs access to locations, catalog read/write, and inventory write for the selected merchant. No live Square calls have been made during this change.

```sh
python roca_to_square.py --csv cards.csv --sandbox --square-location YOUR_SANDBOX_LOCATION_ID
```

Omit `--sandbox` only when ready to write production inventory. One active location is automatically selected; multiple locations require `--square-location` or `SQUARE_LOCATION_ID`.

This is an **initial import**, not a restocking/synchronization tool. Existing SKUs stop the import so reruns cannot silently duplicate items or reset stock after sales. SKU generation in the original CSV parser includes row sequence; reordering or changing exports can change SKUs. Do not reimport overlapping exports. If an upload partially succeeds or times out, inspect Square's catalog and inventory before trying again. Catalog creation and stock assignment are separate Square operations; errors report partial success. Existing items must be reconciled separately. The preflight duplicate check is not a lock: do not run simultaneous imports of the same cards.

## Desktop interface — release 1.1.0

Download the current GitHub ZIP and **extract it into a new folder**. Updating GitHub does not update Python files already downloaded on your computer. Close all older Roca/Python station windows first, then:

1. Run `Install dependencies.cmd` once on Windows.
2. Run `Start Roca.cmd` from the extracted folder (or double-click the new `roca_to_square.py`).
3. Verify the browser shows **Release 1.1.0 • Chrome launcher • compact 12-digit barcodes**.
4. Choose your CSV, printer DPI and standard/fold-over layout, then select **Create labels only** for a test print.
5. Open the newly generated PDF from the Result area. Old PDFs are not updated. Generated files are kept under `outputs/` with unique names.
6. To upload inventory, enter the Square token, select Sandbox or Production, enter the receiving location ID if needed, and select the confirmation checkbox before clicking **Create labels + upload inventory**.

The complete `roca_square_label_app.html` is now included and uses the Python backend for both parsing and label generation. Keep it beside the Python script. The interface shows the exact 12-digit barcode/Square SKU and reports partial inventory errors. Square uploads include the SKU only, with **no GTIN or UPC field**.

Chrome is found through PATH, standard install folders and Windows App Paths registration. If Chrome is missing, the station prints instructions and stays available for opening manually in Chrome; it never opens Edge or the system default browser. The launcher binds its port before opening Chrome, and stops with an explicit message if an older station is using the port. Check the console for its version and script path if you may have multiple downloaded copies.

New settings are saved in `roca_settings.json`: `labelLayout` (`standard` or `fold`), `printerDpi` (203 or 300), `squareLocationId`, and `squareEnvironment` (`sandbox` or `production`). The Square token is stored locally in that file; keep it private. Settings, generated outputs and Python caches are excluded from Git.

## Validation

Nine automated tests exercise mocked Square calls (no real merchant writes) and decode generated PDF barcodes at both supported resolutions. To run the tests, install `requests reportlab pymupdf pillow zxing-cpp pypdf` and run:

```sh
python -m unittest discover -v
```

The browser API was exercised from CSV ingestion through PDF/ZPL downloads, with Square uploads disabled. Chrome detection, prevention of Edge fallback, busy-port handling, PDF page sizes and copy counts were verified. ZPL payloads were checked but have not been physically printed or decoded from a printer. Live Square stock decrement on sale remains to be verified with a sandbox sale or a controlled real-world test.
