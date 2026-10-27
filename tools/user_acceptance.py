"""Act as a cashier in Chrome using an isolated database and no live Square."""
from pathlib import Path
import json
import sys
import tempfile
import threading
from unittest.mock import patch
import traceback

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from playwright.sync_api import sync_playwright, expect
import roca_to_square as core
from station_service import StationService

REPORT=ROOT/'Testing'/'2.6.0';REPORT.mkdir(parents=True,exist_ok=True)
steps=[]
def passed(text):
    steps.append('PASS '+text);print(steps[-1],flush=True)

def run():
    with tempfile.TemporaryDirectory() as folder,patch.object(core,'SETTINGS_FILE',str(Path(folder)/'settings.json')),patch.object(core,'OUTPUT_DIR',str(Path(folder)/'outputs')),patch.object(core.requests,'get',side_effect=AssertionError('Live HTTP forbidden')),patch.object(core.requests,'post',side_effect=AssertionError('Live HTTP forbidden')):
        server=core.HTTPServer(('127.0.0.1',0),core.RocaStationHandler)
        service=server.station=StationService(core)
        service.auth.set_password('fixture-owner-password-only')
        worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
        try:
            with sync_playwright() as pw:
                browser=pw.chromium.launch(channel='chrome',headless=True)
                page=browser.new_page(viewport={'width':1440,'height':1000})
                errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                page.goto(f'http://127.0.0.1:{server.server_port}')
                expect(page.locator('#version')).to_have_text('2.6.0')
                expect(page.locator('.brand')).to_contain_text('Stock & Sleeve')
                passed('Launch shows the correct version, branding and navigation')
                page.locator('#creditNav').click()
                page.locator('#creditAddCustomer').click()
                page.locator('#creditCustomerName').fill('Fictional Pilot Customer')
                page.locator('#creditSquareReference').fill('fixture-customer')
                page.locator('#creditCustomerForm button[type=submit]').click()
                expect(page.locator('#creditAccount')).to_contain_text('Fictional Pilot Customer')
                passed('Create customer from the Store credit screen')
                page.locator('#recordCreditAdjustment').click()
                page.locator('#creditEntryAmount').fill('50')
                page.locator('#creditEntryReference').fill('TEST OPENING')
                page.locator('#creditAdminPassword').fill('fixture-owner-password-only')
                page.locator('#creditEntryForm button[type=submit]').click()
                expect(page.locator('.credit-balance-header strong')).to_have_text('$50.00')
                passed('Manager-authorized opening balance is $50.00')
                page.locator('#recordCreditUse').click();page.locator('#creditEntryAmount').fill('20')
                page.locator('#creditEntryReference').fill('TEST SALE 1')
                page.locator('#creditEntryForm button[type=submit]').click()
                expect(page.locator('.credit-balance-header strong')).to_have_text('$30.00')
                passed('Manual local redemption updates balance to $30.00 and records history')
                page.screenshot(path=str(REPORT/'credit-customer.png'),full_page=True)
                page.reload();page.locator('#creditNav').click()
                page.locator('[data-credit-customer]').click()
                expect(page.locator('.credit-balance-header strong')).to_have_text('$30.00')
                passed('Balance survives a full page reload')
                page.locator('#recordCreditUse').click();page.locator('#creditEntryAmount').fill('31')
                page.locator('#creditEntryReference').fill('TEST OVERDRAFT')
                page.locator('#creditEntryForm button[type=submit]').click()
                expect(page.locator('#creditEntryMessage')).to_contain_text('Insufficient')
                page.locator('#creditCloseEntry').click()
                expect(page.locator('.credit-balance-header strong')).to_have_text('$30.00')
                passed('Insufficient-balance operation is rejected without a debit')
                page.locator('#previewConnections').click()
                expect(page.locator('#previewStatus')).to_contain_text('Automatic Square redemption and shared balances are not connected')
                page.locator('#loadCleanupPreview').click()
                expect(page.locator('#cleanupPreviewResult')).to_contain_text('Unlock Admin')
                passed('Cleanup requires manager authentication and accurately labels live-feature limits')
                page.screenshot(path=str(REPORT/'connections-cleanup.png'),full_page=True)
                assert not errors,errors
                passed('No uncaught browser JavaScript errors')
                browser.close()
        finally:
            server.shutdown();server.server_close();worker.join(5)

try:
    run();steps.append('LIVE SQUARE: NOT CONTACTED. Central hosting, live OAuth, tax and physical hardware: NOT VERIFIED.')
except Exception:
    steps.append('FAIL\n'+traceback.format_exc());raise
finally:
    (REPORT/'user-acceptance.log').write_text('\n'.join(steps)+'\n',encoding='utf-8')
