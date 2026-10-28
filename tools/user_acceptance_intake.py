"""Real Chrome cashier intake acceptance with isolated fictional data only.

All browser navigation is loopback; Python HTTP and Square writes are forbidden.
Product failures are retained in logs/screenshots and do not get patched here.
"""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import traceback
from unittest.mock import patch
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from playwright.sync_api import sync_playwright, expect
import roca_to_square as core
from station_service import StationService

REPORT = ROOT / 'Testing' / '2.6.0'
SOURCE_NAMES = ('roca_to_square.py', 'station_service.py', 'station_server.py', 'station.js',
                'intake.js', 'station.css', 'roca_square_label_app.html', 'scanner_adapters.py',
                'tools/user_acceptance_intake.py')
GAMES = ('pokemon', 'magic', 'riftbound', 'onepiece', 'unionarena', 'gundam', 'sports')


def fingerprints():
    return {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in SOURCE_NAMES}


def run():
    REPORT.mkdir(parents=True, exist_ok=True)
    results = [];steps = [];browser_errors = [];external_attempts = [];backend_attempts = []
    started = datetime.now(timezone.utc).isoformat();source_start = fingerprints()
    def record(message):
        steps.append(message);print(message, flush=True)
    def backend_forbidden(session, method, url, **kwargs):
        backend_attempts.append({'method': str(method), 'host': urlsplit(str(url)).hostname})
        raise AssertionError('Backend network requests are forbidden in this intake walkthrough.')
    with tempfile.TemporaryDirectory() as folder, \
         patch.object(core, 'SETTINGS_FILE', str(Path(folder) / 'settings.json')), \
         patch.object(core, 'OUTPUT_DIR', str(Path(folder) / 'outputs')), \
         patch.object(core.requests.sessions.Session, 'request', backend_forbidden), \
         patch.object(core, 'upload_to_square', side_effect=AssertionError('Square writes are forbidden.')) as square_write:
        server = core.HTTPServer(('127.0.0.1', 0), core.RocaStationHandler)
        service = server.station = StationService(core)
        service.store.put('preferences', service.preferences() | {'pricingEnabled': False})
        worker = threading.Thread(target=server.serve_forever, daemon=True);worker.start()
        base = f'http://127.0.0.1:{server.server_port}'
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(channel='chrome', headless=True)
                context = browser.new_context(viewport={'width': 1440, 'height': 1050})
                def browser_route(route):
                    uri = urlsplit(route.request.url)
                    if uri.scheme == 'http' and uri.hostname == '127.0.0.1' and uri.port == server.server_port:
                        route.continue_()
                    else:
                        external_attempts.append({'scheme': uri.scheme, 'host': uri.hostname})
                        route.abort()
                context.route('**/*', browser_route)
                page = context.new_page()
                page.set_default_timeout(10_000)
                page.on('dialog', lambda dialog: dialog.accept())
                page.on('pageerror', lambda error: browser_errors.append(str(error)))
                def reset():
                    page.goto(base, wait_until='domcontentloaded')
                    expect(page.locator('#version')).to_have_text('2.6.0')
                    expect(page.locator('#addManualItem')).to_be_visible()
                    page.locator('#newReceipt').click()
                    expect(page.locator('#emptyState')).to_be_visible()
                def active_receipt():
                    identifier = page.locator('#receiptPicker').input_value()
                    if not identifier:raise AssertionError('The UI did not select its saved receipt.')
                    return service.store.receipt(identifier)
                def add_manual(game, name, price='2.13', metadata=None):
                    page.locator('#addManualItem').click()
                    expect(page.locator('#manualItemDialog')).to_be_visible()
                    expect(page.locator('#manualGame')).to_be_enabled()
                    page.locator('#manualGame').select_option(game if game in GAMES else 'other')
                    if game not in GAMES:page.locator('#manualGameLabel').fill(game)
                    page.locator('#manualName').fill(name)
                    page.locator('#manualPrice').fill(price)
                    page.locator('#manualQty').fill('1')
                    if not page.locator('#manualSet').is_visible():
                        page.locator('#manualItemForm').get_by_text('More item details', exact=True).click()
                    page.locator('#manualSet').fill('Fictional Set')
                    page.locator('#manualNumber').fill('001')
                    if metadata:
                        if not page.locator('#manualGrader').is_visible():
                            page.locator('#manualItemForm details.intake-identity summary').click()
                        for key, value in metadata.items():page.locator('#manual' + key).fill(value)
                    with page.expect_response(lambda response: response.url.endswith('/api/manual-items') and response.request.method == 'POST') as response_info:
                        page.locator('#manualItemForm button[type=submit]').click()
                    response = response_info.value
                    if response.status != 200:raise AssertionError(f'Manual item rejected: HTTP {response.status}: {response.text()}')
                    expect(page.locator('#manualItemDialog')).to_be_hidden()
                    expect(page.locator('#receiptPicker')).to_have_value(response.json()['receipt']['id'])
                    return active_receipt()
                def reopen(identifier):
                    page.reload(wait_until='domcontentloaded')
                    expect(page.locator('#version')).to_have_text('2.6.0')
                    page.locator('#openHistory').click()
                    expect(page.locator('#historyDialog')).to_be_visible()
                    page.locator('#historyList [data-open="' + identifier + '"]').click()
                    expect(page.locator('#receiptWorkspace')).to_be_visible()
                def open_card(name):
                    page.get_by_role('button', name='Edit ' + name, exact=True).click()
                    expect(page.locator('#cardDrawer')).to_be_visible()
                def save_card():
                    with page.expect_response(lambda response: '/api/receipts/' in response.url and response.request.method == 'POST' and response.url.rstrip('/').split('/')[-1].startswith('R-')) as response_info:
                        page.locator('#cardForm button[type=submit]').click()
                    response = response_info.value
                    if response.status != 200:raise AssertionError(f'Card save rejected: HTTP {response.status}: {response.text()}')
                    expect(page.locator('#cardDrawer')).to_be_hidden()
                def case(name, callback):
                    before = time.monotonic()
                    try:
                        reset();callback()
                        results.append({'case': name, 'status': 'pass', 'elapsed_seconds': round(time.monotonic() - before, 3)})
                        record('PASS ' + name)
                    except Exception:
                        details = traceback.format_exc()
                        image_name = 'intake-failure-' + str(len(results) + 1) + '.png'
                        try:page.screenshot(path=str(REPORT / image_name), full_page=True)
                        except Exception:image_name = None
                        results.append({'case': name, 'status': 'fail', 'elapsed_seconds': round(time.monotonic() - before, 3),
                                        'error': details, 'screenshot': image_name})
                        record('FAIL ' + name + '\n' + details)

                def required_games_and_mixed_batch():
                    page.locator('#addManualItem').click()
                    options = page.locator('#manualGame option').evaluate_all('(options) => options.map(option => option.value)')
                    assert set(GAMES) <= set(options), options
                    page.locator('#cancelManualItem').click()
                    receipt = None
                    for index, game in enumerate(GAMES, 1):
                        receipt = add_manual(game, 'Fictional Shared Name')
                        expect(page.locator('#tableCount')).to_have_text(str(index))
                    receipt = add_manual('Flesh and Blood', 'Fictional Additional Game')
                    expect(page.locator('#tableCount')).to_have_text('8')
                    assert {card['game'] for card in receipt['cards']} == {*GAMES, 'other:flesh-and-blood'}
                    assert receipt['mixed_games'] is True and receipt['pricing_game'] == ''
                    assert len({card['sku'] for card in receipt['cards']}) == 8
                    expect(page.locator('#reviewContinue')).to_be_disabled()
                    expect(page.locator('#labelsOnly')).to_be_disabled()
                    page.screenshot(path=str(REPORT / 'intake-mixed-games.png'), full_page=True)
                    identifier = receipt['id'];reopen(identifier)
                    expect(page.locator('#tableCount')).to_have_text('8')
                    assert active_receipt()['mixed_games'] is True

                def slab_reload_review_and_labels():
                    metadata = {'Grader': 'PSA', 'Grade': '10', 'CertNumber': '0000123456', 'SerialNumber': '03/10',
                                'Year': '2024', 'Manufacturer': 'Fictional Brand', 'Team': 'Fictional Team', 'Player': 'Fictional Player'}
                    receipt = add_manual('sports', 'Fictional Slab', '85.13', metadata)
                    expect(page.locator('#cardRows')).to_contain_text('PSA 10')
                    expect(page.locator('#reviewContinue')).to_be_disabled()
                    expect(page.locator('#labelsOnly')).to_be_disabled()
                    response = page.request.post(base + '/api/receipts/' + receipt['id'] + '/labels', data={})
                    assert response.status == 400, response.text()
                    assert not active_receipt()['files']
                    reopen(receipt['id']);open_card('Fictional Slab')
                    page.locator('#cardForm details.intake-identity summary').click()
                    for key, value in metadata.items():expect(page.locator('#edit' + key)).to_have_value(value)
                    expect(page.locator('#editGame')).to_have_value('sports')
                    expect(page.locator('#editPrice')).to_have_value('85.13')
                    page.screenshot(path=str(REPORT / 'intake-slab-details.png'), full_page=True)
                    page.locator('#editReviewed').check();save_card()
                    expect(page.locator('#reviewContinue')).to_be_enabled()
                    page.locator('#reviewContinue').click()
                    page.locator('[data-intent="existing"]').click()
                    expect(page.locator('#primaryAction')).to_be_enabled()
                    page.locator('#primaryAction').click()
                    expect(page.locator('#outputFiles')).to_contain_text('Open label PDF')
                    final = active_receipt()
                    assert final['inventory_status'] == 'not_received'
                    assert final['cards'][0]['cert_number'] == '0000123456'
                    path = Path(core.OUTPUT_DIR) / final['files']['pdf']
                    import pymupdf
                    with pymupdf.open(path) as document:
                        assert len(document) == 1
                        text = document[0].get_text()
                        assert 'PSA 10' in text and '$85.13' in text, text
                    page.screenshot(path=str(REPORT / 'intake-labels-only.png'), full_page=True)

                def missing_price_and_explicit_zero():
                    content = ('Name,Game,Set,Number,Condition,Printing,Language,Market Price,Quantity\n'
                               'Fictional Unpriced A,Gundam,Fictional Set,001,NM,Normal,English,9.99,1\n'
                               'Fictional Unpriced B,Gundam,Fictional Set,002,NM,Normal,English,5.99,1\n')
                    with page.expect_response(lambda response: response.url.endswith('/api/import')) as response_info:
                        page.locator('#csvFile').set_input_files({'name': 'fictional-unpriced.csv', 'mimeType': 'text/csv', 'buffer': content.encode()})
                    response = response_info.value
                    assert response.status == 200, response.text()
                    expect(page.locator('#cardRows .credit-debit')).to_have_count(2)
                    expect(page.locator('#cardRows')).to_contain_text('Price required')
                    for checkbox in page.locator('#cardRows [data-review]').all():
                        expect(checkbox).to_be_disabled();expect(checkbox).not_to_be_checked()
                    expect(page.locator('#reviewContinue')).to_be_disabled()
                    expect(page.locator('#labelsOnly')).to_be_disabled()
                    if page.locator('#markReviewed').is_enabled():
                        page.locator('#markReviewed').click()
                        if page.locator('#confirmDialog').is_visible():page.locator('#confirmOkay').click()
                    for checkbox in page.locator('#cardRows [data-review]').all():expect(checkbox).not_to_be_checked()
                    expect(page.locator('#reviewContinue')).to_be_disabled()
                    page.screenshot(path=str(REPORT / 'intake-price-required.png'), full_page=True)
                    open_card('Fictional Unpriced A')
                    expect(page.locator('#editPrice')).to_have_value('')
                    expect(page.locator('#editReviewed')).to_be_disabled()
                    page.locator('#editPrice').fill('4.13')
                    expect(page.locator('#editReviewed')).to_be_enabled()
                    page.locator('#editReviewed').check();save_card()
                    open_card('Fictional Unpriced B')
                    expect(page.locator('#editReviewed')).to_be_disabled()
                    page.locator('#editPrice').fill('0')
                    expect(page.locator('#editZeroConfirmation')).to_be_visible()
                    expect(page.locator('#editReviewed')).to_be_disabled()
                    page.locator('#editPriceConfirmed').check()
                    expect(page.locator('#editReviewed')).to_be_enabled()
                    page.locator('#editReviewed').check();save_card()
                    expect(page.locator('#cardRows .credit-debit')).to_have_count(0)
                    expect(page.locator('#reviewContinue')).to_be_enabled()
                    receipt = active_receipt()
                    assert all(card['price_missing'] is False for card in receipt['cards'])
                    assert [card['final_price'] for card in receipt['cards']] == [4.13, 0]
                    assert receipt['cards'][1]['price_confirmed'] is True
                    reopen(receipt['id'])
                    expect(page.locator('#cardRows')).to_contain_text('$4.13')
                    expect(page.locator('#cardRows')).to_contain_text('$0.00')
                    expect(page.locator('#reviewContinue')).to_be_enabled()
                    page.screenshot(path=str(REPORT / 'intake-prices-confirmed.png'), full_page=True)

                case('All required games and an additional game create a mixed manual batch with distinct SKUs and reload persistence', required_games_and_mixed_batch)
                case('Slab grade/certificate/sports fields survive reload and labels stay blocked until reviewed; labels-only does not receive stock', slab_reload_review_and_labels)
                case('Unpriced CSV remains blocked until a positive price or explicit zero confirmation is saved and survives reload', missing_price_and_explicit_zero)
                if browser_errors:record('BROWSER ERRORS\n' + '\n'.join(browser_errors))
                assert square_write.call_count == 0, 'Square upload was attempted.'
                assert not backend_attempts, backend_attempts
                assert not external_attempts, external_attempts
                assert not browser_errors, browser_errors
                record('PASS no external browser requests, backend HTTP, Square uploads or uncaught JavaScript errors')
                browser.close()
        finally:
            server.shutdown();server.server_close();worker.join(5)
            source_end = fingerprints()
            source_stable = source_start == source_end
            if not source_stable:record('EVIDENCE NEEDS RETEST: source files changed during the walkthrough.')
            final = {'started_utc': started, 'finished_utc': datetime.now(timezone.utc).isoformat(),
                     'scope': 'Real headless Chrome, loopback UI, isolated fictional profile; no live Square or external provider traffic.',
                     'sources_before': source_start, 'sources_after': source_end, 'source_stable': source_stable, 'cases': results,
                     'browser_errors': browser_errors, 'external_browser_attempts': external_attempts,
                     'backend_http_attempts': backend_attempts, 'square_upload_attempts': square_write.call_count}
            (REPORT / 'intake-user-acceptance.json').write_text(json.dumps(final, indent=2), encoding='utf-8')
            (REPORT / 'intake-user-acceptance.log').write_text('\n'.join(steps) + '\n', encoding='utf-8')
    if not source_stable or not results or any(result['status'] != 'pass' for result in results):raise SystemExit(1)


if __name__ == '__main__':run()
