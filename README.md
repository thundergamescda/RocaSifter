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

## Existing desktop interface

The desktop launcher prefers Google Chrome, including its standard Windows installation locations. If Chrome is unavailable, it opens the system default browser.

The original script depends on `roca_square_label_app.html`, which was not supplied. Keep your existing HTML beside this updated Python file to use that interface. The CLI works without it. To use new options with the existing interface, retain all current fields in `roca_settings.json` and add:

```json
{
  "labelLayout": "fold",
  "printerDpi": 203,
  "squareLocationId": "YOUR_LOCATION_ID",
  "squareEnvironment": "sandbox"
}
```

Use `"standard"` for an unfolded layout. If the HTML posts its own settings object, it must include these fields; otherwise backend defaults apply. The backend now returns `errors`, `inventoryCount`, and a `partial_error` status when needed. Updating interface controls and displaying those errors requires the original HTML file. Preserve the original script's local settings security model: it stores its Square token in its settings file, so keep that file private.

## Validation

Five automated tests exercise mocked Square calls (no real merchant writes) and decode generated PDF barcodes at both supported resolutions. To run the tests, install `requests reportlab pymupdf pillow zxing-cpp` and run:

```sh
python -m unittest discover -v
```

PDF page sizes and copy counts were verified. ZPL payloads were checked but have not been physically printed or decoded from a printer. Live Square stock decrement on sale remains to be verified with a sandbox sale or a controlled real-world test.
