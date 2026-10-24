# Pokémon pricing: implementation and data-access checkpoint

Research date: October 24, 2026. The installed application is still version 2.3.0.
No pricing subscription, API account, or live lookup has been purchased or activated.

## Work completed

`pricing_review.py` implements an offline comparison engine, with tests in
`test_pricing_review.py`. It is deliberately not imported by the running application.
It does not change prices, generate labels, contact providers, or update inventory.
Import-time automatic checks, the pricing UI, and finish-triggered live repricing
are not connected yet. They remain blocked on verified source coverage and access.

The engine requires independently supplied evidence for:

1. TCGplayer condition/printing-specific market price.
2. TCGplayer completed sales.
3. TCGplayer current listings.
4. eBay completed sales.
5. eBay current listings.

Every Pokémon row receives a result. A row without an identified game or a complete
identity needs identification; it cannot silently be treated as a non-Pokémon card.
Manual items keep the operator's price.

An exact match includes game, name, set, collector number, finish, condition, and
language. The rules retain gender symbols, collector-number denominators, edition,
stamps and premium finish names. Normal, Holofoil, Reverse Holofoil, Poké Ball,
Master Ball, first edition, and unlimited printings must not be pooled.

Each source carries its identity, source record/link, currency, item-price basis,
observation time, sale date where applicable, and exclusions. Missing fields are
unknown, rather than assumed correct. Lots, slabs, signed/altered cards, proxies,
shared-attribution sales and unverified paid amounts require review. Foreign
currencies and prices including different shipping/tax bases are not mixed.

The initial comparison policy requires a source snapshot no older than 36 hours,
at least three unique completed sales from each sales source within 30 days,
and complete five-source coverage. Asking prices are displayed separately from
completed sales and do not enter the proposed price. TCGplayer's exact market
price can become a proposal only if both sold medians agree within 20%, with
no large spreads/outliers. These are proposed store rules, not provider facts
or a guarantee of a future sale. Staff approval remains necessary.
Current listing medians more than 50% away from market also flag review; asking
prices still do not set the selling price. Source links must belong to the stated
TCGplayer/eBay marketplace.

## Provider candidates and cost status

| Provider | Verified from the provider's published SDK | Cost information verified here | Gaps before activation |
| --- | --- | --- | --- |
| [Pkmn Prices](https://www.pkmnprices.com/pricing) | Condition/variant-specific market prices, TCGplayer current listings, eBay sold records, language/grade distinctions and source attribution. | Its SDK documents a free English-data tier and Pro access for other languages/EUR. Exact paid prices, included listing entitlements and credit limits were not accessible. No paid amount is quoted. | Individual TCGplayer sales and eBay current listings are not documented in this SDK. eBay raw sold records lack an explicit raw-card condition field. Confirm freshness semantics, business use, coverage, pricing and credits with the provider. |
| [PokeTrace](https://poketrace.com) | Condition tiers, TCGplayer market/low/mid/high, eBay sold averages and individual completed-sale records. | Its SDK labels market prices/sold averages Free and individual sold records/history Pro. Exact paid prices and request limits were not accessible. No paid amount is quoted. | It does not document every required source. Finish coverage is an explicit enum; confirm premium Pokémon variants and completeness. |

The API hosts and provider pricing pages were blocked by this execution
environment's proxy. Requesting network permissions did not remove that proxy
restriction. Public GitHub SDK documentation was readable. The providers'
claims were reviewed; live responses and data accuracy were **not** validated.

Source documentation read:

- [Pkmn Prices official Python SDK](https://github.com/preaverage/pkmnprices-py)
  (reviewed at commit `dc6bfeb`) and its models, endpoints and tests. README documents per-row credit charging,
  `snapshot_at` for TCGplayer-listing freshness, `exact/shared/unknown` eBay
  attribution, and older accepted-offer records whose true paid price may be unknown.
- [PokeTrace official TypeScript SDK](https://github.com/PokeTrace/sdk)
  (reviewed at commit `cccfdc2`) and its types. README distinguishes free aggregated data from Pro individual sales.
- [TCG Price Lookup official Python SDK](https://github.com/TCG-Price-Lookup/tcglookup-py)
  also documents market prices and paid eBay averages, but does not establish the
  complete five-source coverage requested here.

SDK source-code licenses do not establish the terms or entitlement of the
underlying price feed. The selected provider's actual plan must be checked before
using its data in this store application or distributing it to clients.

## Remaining implementation dependencies

No current candidate is verified to supply all five requested data sources.
An API key alone would not establish complete coverage. Obtain the actual plan
price/credit allowance and either a provider that supplies the missing sources or
separate approved access. Do not substitute market-price aggregates for actual
TCGplayer sales, eBay asking prices for sold prices, or an unknown raw condition
for Near Mint.

Before enabling background checks:

- Confirm whether exports are separated by game. Import game/product identifiers
  when present, or set the batch's game explicitly. Unknown game/set/number must
  become a visible matching task.
- Save keys in password-protected Admin using the existing encrypted-vault design.
  Never embed keys in the repository or return them to operators.
- Add a durable background queue and cache keyed by exact identity and source
  timestamps, with a visible daily credit budget, provider quota handling and
  request deduplication. No paid overage or plan upgrade is automatic.
- Keep results separate from receipt revisions so background writes cannot
  overwrite an operator's finish/price edits. Discard results whose identity
  changed while a lookup was running. A finish/condition/language/set change
  must invalidate the old comparison and trigger a new one.
- Show source coverage, timestamps, excluded comparisons and disagreements in
  the card editor. A missing source must say unavailable. A checkbox marking
  physical review must not silently approve incomplete price evidence.
- Validate a small store sample against actual source pages: common cards,
  high-value cards, vintage/first edition, reverse-holo, premium ball patterns,
  Japanese cards, played cards and uncertain accepted offers. Only then activate
  import-time checks for all Pokémon cards.

This checkpoint preserves the working export pickup, manual items, trade-ins,
labels, inventory receiving and updater. There is no new application release
claiming that live multi-source pricing has been activated.

## Verify the completed offline rules

```sh
python -m unittest test_pricing_review -v
```

The fixtures use fictional prices and source records. They verify rules and
failure handling; they are not market-price measurements or live integration tests.
Sixteen comparison-rule tests passed; the complete suite passed 104 tests.
