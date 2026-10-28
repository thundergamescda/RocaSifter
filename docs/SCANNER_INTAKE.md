# Scanner and manual intake contract

This implementation validates file formats. It does not certify a physical
scanner or establish commercial pricing-feed rights. Real vendor exports and
hardware acceptance remain release requirements.

## Implemented formats

- Roca-style CSV: existing Card Name/Name, Card #/Number, Condition, Printing,
  Language, Set/Set Code, Price/Listing Price, Bin/Bin Number and Qty/Quantity.
- TCGplayer app CSV: the documented SKU, Name, Product ID, Language, Printing,
  Condition and Quantity columns. The SKU is a **TCGplayer identifier**, kept
  under source_ids; it is never automatically used as a Square SKU.
- TCGplayer inventory CSV: Product Line, Product Name, Set Name, Number,
  Condition, marketplace/market price and additive or snapshot quantities.
  A condition that explicitly includes foil printing is split into condition
  and printing, with conflicting explicit printing rejected.
- Canonical or mapped UTF-8 CSV, TSV and semicolon CSV. Custom source columns
  can be mapped to the supported fields; this is the extension point for other
  scanners. No unsupported vendor format is claimed by name.

The TCGplayer app and inventory column names and differing quantity meanings
are grounded in TCGplayer's [CSV import documentation](https://help.tcgplayer.com/hc/en-us/articles/21514024512279-Importing-CSVs-into-BinderPOS)
and [inventory CSV glossary](https://help.tcgplayer.com/hc/en-us/articles/115002358027-Importing-and-Exporting-CSVs-to-Mass-Update-Prices-and-Quantities).
Their [Scan & Identify guide](https://help.tcgplayer.com/hc/en-us/articles/27303403772823-How-To-Use-Scan-Identify)
also explains Roca bin and import-sequence references. Scanner sequence and
image filenames are preserved as source references; the app does not open or
fetch those files automatically.

## Games and slabs

Known keys are pokemon, magic, riftbound, onepiece, unionarena, gundam and sports.
Additional game names use other:<slug> plus their original game_label; they are
not relabeled as Pokémon or Magic to obtain a price. A file can contain per-row
games. Mixed batches retain explicit row identities rather than one guessed game.
Support for intake is separate from verified market-pricing coverage.

Manual cards and slabs pass the same strict quantity, price and identity rules.
Slab data includes grader, grade, qualifier and textual certificate number;
serial number, year, manufacturer, team and player are retained for sports and
other variants. Leading zeroes in collector numbers, certificates and source
identifiers survive. A certificate identifies one physical slab, so a row with
a certificate must have quantity one. Non-certified same-grade lots can still
use quantities when the operator intends that sale unit.

## Safety and review behavior

- Invalid rows block the entire import. Missing names and malformed rows are
  errors; no row silently disappears. Repeated alias columns must agree or be
  explicitly mapped. Files and row/field counts have bounded limits.
- Quantities are positive integers. Add to Quantity, Total Quantity and Set
  Stock are different concepts. Mixed meanings require choosing a column.
  Snapshot quantities require explicit confirmation and create only a draft.
- Negative, nonfinite, exponential, malformed and spreadsheet-formula prices
  are rejected. Prices are USD and exact to cents. A market reference remains
  separate from a selling price.
- Missing selling prices remain visible and review-required. The current UI
  stores a numeric display placeholder with price_missing=True; it cannot pass
  review or the label/receiving/trade gates. A positive price resolves it; a
  genuine zero price needs explicit price_confirmed=True.
- Generic source SKUs are preserved separately. Only an explicit Square SKU
  maps to the existing 12-digit Square override. Existing receipt overrides and
  legacy identities are preserved; new explicit game/slab/sports identities use
  the extended generate_card_sku fingerprint. No automatic Square migration or
  deletion happens during intake.
- Stored names are not mutated to escape spreadsheet formulas. CSV export
  boundaries add an apostrophe to dangerous spreadsheet cells instead.
- Defaults for missing condition/printing/language/quantity generate review
  warnings. Each imported row starts reviewed=False; client-supplied review
  flags cannot bypass manual-entry review.

## Integration points and evidence

scanner_adapters.parse_csv returns ImportPreview (cards, issues, adapter and
quantity semantics); parse_csv_strict rejects any error. normalize_manual_card
shares validation with manual entry. StationService.parse/import_csv and
add_manual_item use these adapters; validate_card/save_receipt preserve the
metadata and price gates. Core parse_roca_csv uses strict adapters but keeps
legacy command-line quarter-dollar rounding and rejects missing sale prices.

The service accepts intake_options with adapter, default_game, column_mapping,
quantity_column, price_column and confirm_snapshot. UI/API callers must expose
those deliberate mapping/confirmation choices before claiming custom-vendor
onboarding is complete. Per-card source IDs are informational, never proof of
exact pricing identity or permission to change marketplace data.

Tests: test_scanner_adapters.py and test_intake_integration.py cover all named
games, additional games, slab/serial distinctions, identifier namespaces,
strict validation, price and stock-snapshot gates, atomic imports, receipt-save
preservation and legacy SKU compatibility. The existing station/core/variation
regression suite also passes against fictional responses. Real scanner exports,
physical printer/scanner roundtrips and operator usability are still unproven.
