# Direct-source pricing: implementation status

Updated October 24, 2026. Target sources: **eBay, PriceCharting and TCGplayer**.
Batches are separated by game. No pricing subscription or API feed has been purchased or activated.

## Available in Roca Station 2.3.1

**Compare prices** appears in Add item and the card editor. It uses the current
form values, including unsaved corrections, to prepare four direct source searches:

- eBay completed/sold listings.
- eBay current listings.
- PriceCharting's price-guide search.
- TCGplayer product search for its market, sales and listing information.

Choose Pokémon or Magic in the comparison dialog. Searches include name, set,
collector number, finish, language and raw condition or slab grade. The links are
navigation aids: the source sites may return multiple or incorrect matches. Staff
must check the exact item, printing and condition/grade on the source page.

For slabs, TCGplayer is labeled a raw-card reference. Returning from comparison
preserves the form and selling price. **No live prices are fetched or verified,
and opening a search never changes a price or inventory.** Staff enter their
chosen selling price and use the existing review/Finish workflow.

No API key is needed to open the searches. The source websites can have their own
login or account requirements. Nothing in this release bypasses those requirements.

## Offline comparison rules implemented and tested

`pricing_review.py` contains the comparison policies. Only its search-link helper
is connected to the running application; its evidence evaluation is not active.

Raw cards compare TCGplayer market, completed sales and current listings, eBay
sold/current listings, and PriceCharting's exact guide value. TCGplayer market is
the proposal anchor. Sold medians and the guide must corroborate it. Current
asking prices provide context and are not averaged into completed sales.

Slabs compare exact-grader/grade/tier eBay sold/current listings and PriceCharting.
Recent sold median is the proposal anchor. Raw TCGplayer prices do not set slab
values. A generic PriceCharting “Grade 9” value cannot be called PSA 9 without an
explicit grader match. PriceCharting can incorporate eBay transactions, so its
guide is not counted as another set of independent completed sales.

Identity includes game, name, set, collector number, finish, condition, language,
and, for slabs, grader, grade and premium tier. Examples that must stay separate:

- Normal, Holofoil, Reverse Holofoil, Poké Ball and Master Ball patterns.
- First edition and unlimited; different collector-number denominators.
- Nidoran female and male; different languages.
- PSA 9 and PSA 10; PSA 10 and BGS 10.
- BGS 10 and BGS Black Label 10.
- CGC Gem Mint 10, Pristine 10 and Perfect 10. Plain “CGC 10” needs clarification.

A separate-game batch can supply its game once, without altering row identities.
Conflicting row/batch game values require identification. Unknown games are not
silently dismissed as non-Pokémon. Manual lookups are explicit, and the engine
never mutates the operator's custom selling price.

Initial proposed review thresholds: current source snapshots within 36 hours,
at least three distinct completed sales per required sales source within 30
days, sales/guide agreement within 20%, and manual review for wide spreads or
asking-price differences over 50%. These are draft store rules, not guarantees.
Every observation must independently verify the exact identity, source link,
record ID, USD item-price basis, date, single-card quantity and exclusions.
Lots, proxies, signed/altered cards, unknown attribution, uncertain accepted-offer
amounts, stale data and mismatched currencies require review.

Missing evidence stays unavailable; it never becomes a zero price or a claim
that a source was checked. A finished proposal still requires operator approval.

## Still required for automatic repricing

Automatic import-time checks and finish-triggered live repricing are not active.
The direct sources' current data-access options, coverage, limits and pricing
need verification, followed by appropriate account/API access and live tests.
No current paid cost has been verified or quoted, and nothing has been purchased.

Relevant direct-source documentation:

- [PriceCharting API documentation](https://www.pricecharting.com/api-documentation)
- [PriceCharting subscription information](https://www.pricecharting.com/pricecharting-pro)
- [eBay Browse API](https://developer.ebay.com/api-docs/buy/browse/overview.html)
- [eBay Marketplace Insights](https://developer.ebay.com/api-docs/buy/marketplace-insights/overview.html)
- [TCGplayer API getting started](https://docs.tcgplayer.com/docs/getting-started)

These sites were blocked by this execution environment's proxy. Their current
plans/entitlements and live responses could not be verified here. Public SDKs
and repository examples are not proof of live access or accurate data. No
PriceCharting price-field-to-grade mapping has been invented from generic API
field names. No public-search result is treated as a verified price.

Before automatic activation, implement verified source adapters, encrypted Admin
credentials where required, a durable lookup queue and identity/source-aware
cache, and a visible usage budget. Background results must not overwrite newer
operator edits. A finish/condition/set/language/grade change must invalidate the
previous comparison. Validate a store sample against real source pages before
rolling out checks for every Pokémon card.

## Verification

```sh
python -m unittest test_pricing_review -v
python -m unittest discover -q
python browser_smoke.py
```

Fixtures use fictional prices and records. They test matching/review rules and
workflow safety; they are not live market measurements. Browser checks inspect
search links and form preservation without visiting or scraping source sites.
