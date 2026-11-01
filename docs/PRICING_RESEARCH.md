# Source access report — rechecked November 1, 2026

Public primary documentation is now reachable. PriceCharting's [API documentation](https://www.pricecharting.com/api-documentation) requires written commercial permission for sharing data through software used by other stores. Its current-value API does not provide historical sale transactions. The [Legendary plan](https://www.pricecharting.com/pricecharting-pro) displays $49/month; that is not a commercial redistribution quote. Keep activation disabled pending written terms, coverage and purge requirements.

TCGplayer's [getting-started page](https://docs.tcgplayer.com/docs/getting-started) currently says new API access is unavailable. [TCGCSV](https://tcgcsv.com/) provides product/printing aggregates without SKU condition/language data; its [FAQ](https://tcgcsv.com/faq) confirms individual listings and sales are unavailable. English/NM filtering does not turn this reference into verified condition-specific evidence. The structured recheck and owner decisions are saved in the project's `Planning/PRICING_ACCESS_RECHECK_2026-11-01.json`.

The October 26 implementation/access history below is retained. Its connection failures do not describe the November 1 documentation recheck. No source was purchased or activated.

The active target is **PriceCharting guide values, TCGplayer recent individual sold prices and TCGplayer lowest matching listings**. eBay is no longer part of the active policy. No subscription or API access has been purchased.

## Current implementation

The 2.5.0 local build has a Price research tab, background jobs, exact-identity caching, separate intake/retail suggestions, discrepancy flags, protected manual prices and immutable accepted buying amounts. It implements the complete-evidence comparison policy using fictional test fixtures. This does not constitute a live connection to all required sources.

The public TCGCSV adapter can supply dated market/low references for exact English Near Mint raw cards. It does not contain verified individual transactions. PriceCharting and TCGplayer sold-record adapters are **not implemented or connected**. The current app cannot fetch them merely by enabling a setting.

## Paid access

PriceCharting provides an API with paid access. Its current plan, price, trading-card/grade coverage, request limits and commercial-use rights need confirmation from its official documentation before purchase. A guide value is not an individual TCGplayer completed-sale record; one subscription should not be assumed to supply all requested signals.

Verified TCGplayer transactions need a separately confirmed source. An API advertising market/history prices or lowest listings is not sufficient evidence that it supplies dated paid-price transactions for the exact finish, condition and language. A provider must demonstrate that coverage with sample responses and permitted app use before selection or payment. No current all-in price has been verified.

The development environment could not reach the official pricing/docs pages during the latest research attempt. Earlier probes returned proxy HTTP 403; the latest probes failed at the proxy. Current plan costs and live schemas remain unverified. Do not treat estimates or passing fixture tests as a live data entitlement.

Official references to check:

- PriceCharting API: https://www.pricecharting.com/api-documentation
- PriceCharting plans: https://www.pricecharting.com/pricecharting-pro
- TCGplayer API access: https://docs.tcgplayer.com/docs/getting-started
- Public daily reference feed: https://tcgcsv.com/

## Before activating a paid source

Confirm the provider, full monthly cost, trading-card and grade coverage, request/bulk limits, exact transaction fields and permitted in-app use. Show the owner the plan and cost before any purchase. Implement the real adapter and test a store sample including premium finishes, played cards and slabs. Only then mark the corresponding source checked.

At the store’s volume, duplicate copies should reuse an identity lookup and recent cached records. Pricing failure must leave the current value intact; source disagreement or a change over 15% and at least $1 must be reviewed. Different sources that reuse the same underlying sales must not be described as independent confirmations.
