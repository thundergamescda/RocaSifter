# Pricing coverage — 2.5.0

Research is in-app. Every imported identity queues a lookup; exact duplicates share a job. Editing finish, condition, language, set or number requests a different identity. A failed lookup retains the current price and shows a reason. Opening research never opens external card-search pages.

| Source | Current access | Automatic correction |
| --- | --- | --- |
| TCGplayer market | Public daily TCGCSV reference; English NM exact matches only | No, partial evidence |
| TCGplayer lowest listing | Public daily snapshot; not a verified individual condition-specific listing | No, partial evidence |
| TCGplayer recent individual sold records | Not connected | Not available |
| PriceCharting exact guide | Not connected | Not available |

The public endpoints were blocked by the development environment proxy. Adapter contracts, matching and expiry are tested using fictional fixtures; current live availability, schema and store match coverage remain unverified. eBay is no longer part of the active pricing policy. No paid feed or subscription has been purchased.

## Price policy

- Current partial market/low suggestions require explicit approval. Missing evidence is never a zero price or a successful check.
- With all three verified sources (TCGplayer recent solds, lowest matching listing and PriceCharting), intake uses the lowest listing times the intake-value percentage. Normal cash/credit buying percentages apply afterward.
- Retail uses 75% recent sold median plus 25% exact guide, then the retail percentage. These initial weights are visible policy, not a promise of appraisal accuracy.
- Sales require at least three distinct verified single-card paid-price records within 30 days. Evidence must match game, set, number, finish, condition, language and raw/graded identity. Sources must be current, USD, non-lot, genuine and attributed to the exact card.
- Differences **over 15% AND at least $1** require review, whether between verified sources or between the proposed/current price. Admin can adjust both thresholds.
- A small correction can apply only with complete verified evidence and automatic correction enabled. It resets the checked box, so the label price is still reviewed before finishing. Manual retail prices, explicit retail approvals and generated retail files are not silently overwritten. Accepted buying amounts stay fixed.
- Pokémon retail prices from $0.50 through $1 become $1 labels. Intake keeps its unrounded value. Received stock never changes through this rule.
- Slabs need exact grader/grade evidence; a raw TCGplayer reference is excluded. No guessed slab prices.

## Remaining source work

Permitted source access and any costs must be shown to the owner before purchase. Real sold/guide adapters and a store sample must be validated before calling this complete multi-source automatic research. The strict evidence integration/policy is implemented and tested, but no live sold/guide connection is shipped. Fixture tests do not activate sources.
