# Staff stock lookup

Owner, manager, buyer and cashier can open **Stock lookup** from the signed-in
workspace. Choose a current active location and enter or scan one exact Square
SKU. Enter and **Check stock** perform the same manual read. The field retains
case, spaces, punctuation and leading zeros; input longer than 100 characters is
rejected without truncation. No matching variation is chosen from an ambiguous
or incomplete search.

The page shows Square-reported `IN_STOCK` quantity at the selected location;
physical stock may differ. **Checked at** is the service's read-completion time.
**Last affecting update** is Square's `calculated_at`, which can be old when
stock has not changed. Times use the reader's locale, retaining the exact source
timestamp in the title/data attribute. After a minute the page calls the result a previous check.
Checking again is manual. This screen does not reserve stock or promise checkout
availability. A missing count stays unknown, while an explicit zero stays a
reported zero. Signed and fractional decimal text is retained without rounding.

## Supported references

Stock lookup has its own price-independent validator. Current, nondeleted
`ITEM_VARIATION` and matching `ITEM` references must be present at the selected
merchant location with inventory tracking enabled there. A selected-location
boolean tracking override takes precedence over the base value; unset tracking
at both levels is disabled. Duplicate or malformed overrides are refused.
Variable prices, non-USD locations/prices, item options, archived items and
non-sellable variations are not rejected merely because they cannot be received
through the receiving workflow. Applicable stockable/sellable/archived/sold-out
flags are shown as qualifications, without changing the returned count.

Measured references read their current `MEASUREMENT_UNIT` for a standard or
custom unit label and precision. Conversion references validate the linked
stockable variation, shared parent, tracking/presence and positive decimal ratio.
The count always remains the requested variation's own count: no conversion,
derived count or substitution of the linked stockable count is performed.
Component-based or converted variations may have no count in this endpoint;
that result is unknown. A returned `is_estimated=true` or missing count timestamp
also stays unknown. Unverifiable unit, option, conversion, presence or tracking
shapes return a specific typed unsupported reason, with no numeric fallback.
Measurement fields must agree with any declared unit type and a supported
standard enum value, or provide a valid custom name/abbreviation. Unknown/open
future types and unknown unit values are refused, rather than guessed. Time
units are accepted without a declared type; unlisted declared time types remain
unsupported until documented. Retry wording matches **Check again** after every
attempt, and edits reset the button to **Check stock**.

## Read boundaries

The fixed staff APIs are `GET /api/stock/locations` and
`GET /api/stock/lookup?sku=...&location_id=...`; `/stock` is the page shell.
Store and staff identity come from the authenticated session. The service checks
current role/session/MFA and saved Square authority around blocking reads.
Catalog and location bookends detect changed context; complete pagination is
still a read interval rather than an atomic snapshot. Changes, malformed or
foreign/duplicate count rows and incomplete reads clear the numeric result.
Input/location edits, account transitions and navigation clear old browser data;
late replies must match the current request and freshly checked principal.

Existing `verified_access` and normal OAuth renewal remain in use. No scopes are
added. Stock reads do not change catalog, inventory, receiving, labels, trades,
credits or payments, and do not collect cleanup history. The count adapter is
bounded to one variation/location, three pages, twenty records, 256 KiB and a
cooperative ten-second deadline. The complete service read has a cooperative
sixty-second deadline; blocking transport calls retain their own finite timeout.
Locations are capped at 100 and 64 KiB; lookup responses at 16 KiB. Overflow is
incomplete, not a truncated stock report.

## Acceptance status

The nine new PostgreSQL/HTTP cases are source definitions and **UNRUN** until
separate review and CI admission. Existing 15 browser flows and 16 captures are
preserved; they do not establish acceptance of this new screen. New real HTTPS
staff-page acceptance, actual BFCache restoration, physical scanner/printer and
live Square behavior remain separate requirements. Fictional provider transport
does not establish device or merchant acceptance.

Primary Square references checked 2026-11-19; existing API pin `2026-09-16` retained:
[counts](https://developer.squareup.com/reference/square/inventory-api/BatchRetrieveInventoryCounts),
[InventoryCount](https://developer.squareup.com/reference/square/objects/InventoryCount),
[variation tracking and stock/sale units](https://developer.squareup.com/reference/square/objects/CatalogItemVariation),
[location overrides](https://developer.squareup.com/reference/square/objects/ItemVariationLocationOverrides),
[measurement units](https://developer.squareup.com/reference/square/objects/CatalogMeasurementUnit),
[conversion](https://developer.squareup.com/reference/square/objects/CatalogStockConversion),
[Square's pinned length enum](https://github.com/square/square-python-sdk/blob/27e0343f88057d1274af88c9150b3e149b9d9489/src/square/types/measurement_unit_length.py)
and [unit type enum](https://github.com/square/square-python-sdk/blob/27e0343f88057d1274af88c9150b3e149b9d9489/src/square/types/measurement_unit_unit_type.py).
