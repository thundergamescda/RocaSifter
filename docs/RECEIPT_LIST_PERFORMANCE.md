# Receipt history and pickup polling

October 28, 2026. These measurements cover local SQLite list polling on fictional
temporary data, not browser rendering, physical scanners/printers, network,
Square operations, or a guarantee for every customer's computer.

The old history and pickup methods fetched all selected receipt bodies at once
and decoded their complete card/purchase details on every poll. History removed
only `cards`, leaving large accepted-purchase item lists and possible legacy
intent payloads in the response. This repeated work was measurable at a store
workload of 150 receipts with 750 rows each.

`Store` now saves a versioned derived summary in the same SQLite transaction as
the original receipt and event. The public shape retains history/status fields,
row/unit/retail calculations, compact accepted-offer metadata and file names.
Accepted-offer scope (hosted versus local) is preserved. A computed
`all_reviewed` flag requires at least one included card and all included cards
reviewed with no missing price; excluded cards do not block that flag.
It excludes full cards, purchase item lists, customer references and intent
payloads. `shared_trade` contains only `state`, `trade_id` and a bounded message.
The complete original receipt is still available when a batch is opened.

Warm polling reads compact cache records and does not acquire a writer lock.
Missing, stale, malformed or overbroad summaries regenerate from one original
receipt at a time under a serialized repair transaction. SQL triggers invalidate
derived rows on direct receipt insert/update/delete, including a replacement
whose timestamp has not changed. Repair does not rewrite original receipts,
financial records, events or receiving journals. Soft-delete/restore behavior,
the 150-record limit and each list's ordering are preserved.

## Representative comparison

Evidence: `Testing/2.6.0/receipt-lists-comparison.json`, recorded at 17:53 UTC.
Machine: Windows 11 build26300, AMD64 Family25 Model97 Stepping2 processor,
Python3.12.14. Three warm samples per method ran under `tracemalloc`; memory
is peak Python allocations per call, excluding SQLite native allocations and
the OS file cache. The fixture database was66.8MB:112,500 fictional card rows,
synthetic identity/quantity/price provenance, and one accepted-purchase item
snapshot per row. Only tiny legacy intent markers were used. Full new shared
trade request bodies belong in the separate encrypted pending journal.

| Measurement | Exact old algorithm reference | Cached method |
| --- | ---: | ---: |
| History median | 2.024s | 8.83ms |
| History peak Python allocation | 95.4MB | 0.54MB |
| Pickup median | 1.966s | 9.22ms |
| Pickup peak Python allocation | 69.0MB | 0.54MB |
| History JSON response | 5.54MB | 73.1KB |

Both methods produced 150 results with identical per-receipt totals:666 included
rows,1,663 units and$20,859.70 retail. This reference is the exact previous
polling algorithm run against the same fictional database, not an older binary.
Cold rebuilding all150 derived summaries took1.473s with1.95MB peak Python
allocation. It is a one-time repair cost; later saves keep the cache current.

The earlier `receipt-lists-before.json` and `receipt-lists-after.json` are a
separate stress experiment with the same row count but two additional full
legacy intent snapshots per receipt (188.8MB database). Those records showed
7.414s versus11.18ms history polling and5.295s versus11.36ms pickup polling.
Do not present those stress timings as the normal current shared-trade data
shape or combine them with the representative comparison's response/memory
figures. Each report includes the tested source hash and a stability check.

Reproduce the representative comparison:

```powershell
.build-venv/Scripts/python.exe tools/benchmark_receipt_lists.py --output Testing/2.6.0/receipt-lists-comparison.json --label representative-cache-and-legacy --compare-legacy
```

## Correctness evidence

`test_receipt_summary_cache.py` covers exact counts/money, compact public fields,
failed-cache/failed-event transaction rollback, stale/malformed cache repair,
legacy schema handling, trigger invalidation, delete/restore and limits/order,
two simultaneous Store instances, and read-only warm polls. It verifies that
repair leaves original body bytes, local customer balances/entries, reservations,
idempotency records and receiving-operation requests/responses unchanged.
Focused logs are in `receipt-cache-tests.log/json`; the full release run must
also pass against the final stable source and packaged browser walkthroughs.
The benchmark source hash predates the final scope/review-flag metadata fields;
those additions have focused correctness assertions and were not separately
retimed. They do not change the original card/price calculations or cache-read
strategy measured here.

One broader invocation initially omitted Python's `-X utf8` and hit existing
Windows cp1252 checkmark-print errors in label generation. Repeating the65 cache,
station and intake tests with UTF-8, as the release runner uses, passed. A later
fixture-only ledger-preservation test initially omitted its synthetic Square
customer link; that rejected reservation was corrected and its failed log is
retained separately. Neither failure was hidden by weakening a product assertion.
The ordering fixture also now supplies unique deterministic timestamps rather
than assuming Windows wall-clock readings differ on every rapid save; its
earlier failed fixture log is retained separately.
