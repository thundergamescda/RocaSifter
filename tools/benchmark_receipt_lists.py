"""Measure receipt-list polling with fictional temporary receipts; no app/provider calls."""
import argparse
import datetime
import gc
import hashlib
import json
import platform
from pathlib import Path
import statistics
import sys
import tempfile
import time
import tracemalloc

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from station_store import Store


def fixture(index, rows):
    cards = [{'row_id': row, 'game': 'pokemon', 'card_name': 'Fictional performance card ' + str(row),
              'set_name': 'Fixture set', 'card_number': str(row), 'condition': 'NM', 'finish': 'Normal',
              'qty': row % 4 + 1, 'final_price': (row % 257 + 1) / 10,
              'include': row % 9 != 0, 'reviewed': True, 'storage_location': 'FIXTURE-A',
              'pricing_evidence': {'source': 'fictional', 'note': 'Synthetic price provenance ' * 8}}
             for row in range(rows)]
    return {'id': f'PERF-{index:04d}', 'title': 'Fictional intake ' + str(index),
            'source_name': 'fictional-scanner-export.csv', 'mode': 'trade', 'cards': cards,
            'pickup_pending': True, 'inventory_status': 'not_received', 'prepared': False,
            'purchase': {'type': 'credit', 'amount': 123.45, 'at': '2026-10-28',
                         'items': [{'row_id': c['row_id'], 'qty': c['qty'], 'intake_value': c['final_price']} for c in cards]},
            'shared_trade': {'state': 'pending', 'trade_id': 'fixture-trade', 'message': 'Fictional fixture',
                             'intent': {'items': [{'row_id': 1, 'qty': 2}]}},
            'intent': {'request': 'fictional-legacy-marker'}, 'files': {}}


def legacy_summaries(store):
    """Exact pre-cache polling algorithm, retained only as a benchmark reference."""
    with store.db() as db:
        rows = db.execute('SELECT body FROM receipts WHERE deleted_at IS NULL ORDER BY updated DESC LIMIT 150').fetchall()
    result = []
    for row in rows:
        receipt = json.loads(row[0])
        cards = [c for c in receipt['cards'] if c.get('include', True)]
        receipt['rows'] = len(cards)
        receipt['units'] = sum(c['qty'] for c in cards)
        receipt['retail'] = round(sum(c['qty'] * c['final_price'] for c in cards), 2)
        receipt.pop('cards')
        result.append(receipt)
    return result


def legacy_pickup(store):
    with store.db() as db:
        rows = db.execute('SELECT body FROM receipts WHERE pickup_pending=1 AND deleted_at IS NULL ORDER BY updated,id LIMIT 150').fetchall()
    result = []
    for row in rows:
        receipt = json.loads(row['body'])
        result.append({key: receipt[key] for key in ('id', 'title', 'source_name', 'mode')})
    return result


def measured(function, repetitions):
    samples = []
    result = None
    for _ in range(repetitions):
        gc.collect()
        tracemalloc.start()
        began = time.perf_counter()
        result = function()
        elapsed = time.perf_counter() - began
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        samples.append({'seconds': elapsed, 'peak_python_bytes': peak})
    return result, {'samples': samples, 'median_seconds': statistics.median(s['seconds'] for s in samples),
                    'max_peak_python_bytes': max(s['peak_python_bytes'] for s in samples)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--label', required=True)
    parser.add_argument('--receipts', type=int, default=150)
    parser.add_argument('--rows', type=int, default=750)
    parser.add_argument('--repetitions', type=int, default=3)
    parser.add_argument('--compare-legacy', action='store_true', help='Also time the exact old polling algorithm against this same fixture')
    args = parser.parse_args()
    if not 1 <= args.receipts <= 300 or not 500 <= args.rows <= 2000 or not 1 <= args.repetitions <= 10:
        parser.error('Use 1-300 fictional receipts, 500-2000 rows and 1-10 repetitions.')
    source_hash = hashlib.sha256((ROOT / 'station_store.py').read_bytes()).hexdigest()
    with tempfile.TemporaryDirectory(prefix='ss-receipt-benchmark-') as temporary:
        store = Store(Path(temporary))
        for index in range(args.receipts):
            store.save_receipt(fixture(index, args.rows))
        sample = fixture(0, args.rows)
        included = [card for card in sample['cards'] if card['include']]
        expected = {'rows': len(included), 'units': sum(c['qty'] for c in included),
                    'retail': round(sum(c['qty'] * c['final_price'] for c in included), 2)}
        summaries, summary_metrics = measured(store.summaries, args.repetitions)
        queue, queue_metrics = measured(store.pickup_queue, args.repetitions)
        count = min(args.receipts, 150)
        correct = (len(summaries) == count and len(queue) == count
                   and all(all(receipt[key] == value for key, value in expected.items()) for receipt in summaries))
        summary_bytes = len(json.dumps(summaries).encode())
        comparison = None
        if args.compare_legacy:
            previous, previous_metrics = measured(lambda: legacy_summaries(store), args.repetitions)
            previous_queue, previous_queue_metrics = measured(lambda: legacy_pickup(store), args.repetitions)
            previous_correct = (len(previous) == count and previous_queue == queue and
                                all(all(r[key] == value for key, value in expected.items()) for r in previous))
            comparison = {'algorithm': 'Exact pre-cache Store.summaries/pickup_queue code, not an old executable.',
                          'summary_poll': previous_metrics, 'pickup_poll': previous_queue_metrics,
                          'response_bytes': len(json.dumps(previous).encode()), 'results_correct': previous_correct}
            correct = correct and previous_correct
        repair = None
        with store.db() as db:
            has_cache = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='receipt_summaries'").fetchone()
            if has_cache:
                db.execute('DELETE FROM receipt_summaries')
        if has_cache:
            repaired, repair = measured(store.summaries, 1)
            correct = correct and len(repaired) == count and all(all(r[key] == value for key, value in expected.items()) for r in repaired)
        database_bytes = store.path.stat().st_size
    stable = source_hash == hashlib.sha256((ROOT / 'station_store.py').read_bytes()).hexdigest()
    report = {'timestamp_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(), 'label': args.label,
              'scope': 'Synthetic local SQLite polling only, not end-to-end UI, network, printer or Square performance.',
              'hardware': {'platform': platform.platform(), 'processor': platform.processor(), 'python': platform.python_version()},
              'workload': {'receipts': args.receipts, 'rows_each': args.rows, 'total_rows': args.receipts * args.rows,
                           'repetitions': args.repetitions, 'database_bytes': database_bytes,
                           'shape': 'Each card has synthetic identity/quantity/price provenance; each accepted purchase has one item snapshot per row. Only tiny legacy intent markers are present; full shared-trade request bodies belong in the encrypted journal.'},
              'expected_per_receipt': expected, 'results_correct': correct, 'response_bytes': summary_bytes,
              'summary_poll': summary_metrics, 'pickup_poll': queue_metrics,
              'legacy_reference': comparison, 'cold_derived_cache_repair': repair,
              'memory_scope': 'tracemalloc peak Python allocations per call; excludes SQLite native allocations and OS cache.',
              'station_store_sha256': source_hash, 'source_stable': stable, 'live_provider_calls': False}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(report, indent=2))
    return 0 if correct and stable else 1


if __name__ == '__main__':
    sys.exit(main())
