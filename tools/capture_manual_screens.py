"""Capture fictional training receipts for the operator manual. No Square writes."""
from pathlib import Path
import csv
import io
import shutil
import sys
import tempfile
import threading
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import roca_to_square as core
from station_service import StationService
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / 'docs' / 'manual-assets'
ASSETS.mkdir(parents=True, exist_ok=True)
rows = [
    ['Snapcaster Mage','0808','Near Mint','Foil','SLD','English','24.75','1','3'],
    ['Sol Ring','0236','Lightly Played','Normal','CMM','English','3.75','1','4'],
    ['Lightning Bolt','0141','Near Mint','Normal','M11','English','1.50','2','2'],
    ['Example premium card','0012','Near Mint','Textured Foil','SET','English','45.00','1','1'],
]
f = io.StringIO();writer = csv.writer(f)
writer.writerow(['Card Name','Card #','Condition','Printing','Set Code','Language','Price','Bin','Qty']);writer.writerows(rows)
CSV = f.getvalue()
with tempfile.TemporaryDirectory() as temp, patch.object(core,'SETTINGS_FILE',str(Path(temp)/'settings.json')),patch.object(core,'OUTPUT_DIR',str(Path(temp)/'outputs')):
    server = core.HTTPServer(('127.0.0.1',0),core.RocaStationHandler)
    service = StationService(core);server.station = service
    receipt = service.import_csv(CSV,'Sample collection.csv','trade')['receipt']
    thread = threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(executable_path=shutil.which('chromium'),headless=True,args=['--no-sandbox'])
            page = browser.new_page(base_url=f'http://127.0.0.1:{server.server_port}',viewport={'width':1440,'height':960},device_scale_factor=1.5)
            errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
            page.goto('/');page.wait_for_selector('#version:text("2.0.4")')
            page.screenshot(path=str(ASSETS/'welcome.png'))
            page.locator('#receiptPicker').select_option(receipt['id']);page.wait_for_selector('#inventoryDialog[open]');page.locator('[data-choose-intent="new"]').click();page.wait_for_selector('#tradePanel:not([hidden])')
            page.evaluate('window.scrollTo(0,0)');page.screenshot(path=str(ASSETS/'workspace.png'))
            page.locator('#tradePanel').screenshot(path=str(ASSETS/'trade-offers.png'))
            page.locator('.card-table-panel').screenshot(path=str(ASSETS/'card-table.png'))
            page.locator('[data-edit]').first.click()
            page.locator('#editFinish').fill('Surge Foil');page.locator('#editPrice').fill('29.75');page.locator('#editReviewed').check()
            page.locator('#cardDrawer').screenshot(path=str(ASSETS/'card-editor.png'))
            page.locator('#closeDrawer').click()
            page.locator('#openAdmin').click()
            page.locator('#adminDialog').screenshot(path=str(ASSETS/'admin-setup.png'))
            # Test password is never rendered as plain text or carried into production data.
            page.locator('#adminPassword').fill('Training-capture-only-246!');page.locator('#confirmPassword').fill('Training-capture-only-246!');page.locator('#authSubmit').click();page.wait_for_selector('#adminSettings:not([hidden])')
            page.locator('#adminDialog').evaluate('(e)=>e.scrollTop=0')
            page.locator('#adminDialog').screenshot(path=str(ASSETS/'admin-settings.png'))
            page.locator('#closeAdmin').click()
            # Prepare a reviewed receipt via the service, with no inventory operation.
            online = service.store.receipt(receipt['id']);online['mode']='online'
            online['storage_strategy']='display';online['storage_location']='Display case 2 · shelf B';online['inventory_intent']='existing'
            for card in online['cards']:card['reviewed']=True
            online = service.save_receipt(online['id'],online)
            page.reload();page.locator('#receiptPicker').select_option(receipt['id']);page.wait_for_selector('#onlinePanel:not([hidden])')
            page.locator('#onlinePanel').screenshot(path=str(ASSETS/'online-storage.png'))
            page.locator('.action-panel').screenshot(path=str(ASSETS/'label-actions.png'))
            service.create_task({'channel':'TCGplayer','order_reference':'SAMPLE-1001','card_name':'Snapcaster Mage · Surge Foil · Near Mint','qty':1,'storage_location':'Display case 2 · shelf B'})
            page.locator('[data-mode="activity"]').click();page.wait_for_selector('.task-card')
            page.locator('#taskList').screenshot(path=str(ASSETS/'pull-task.png'))
            assert not errors,errors
            browser.close()
    finally:
        server.shutdown();server.server_close();thread.join()
# Real label output, using fictional training details and no merchant credential.
with tempfile.TemporaryDirectory() as temp:
    sample = {'card_name':'Snapcaster Mage','card_num':'0808','card_set':'SLD','condition':'Near Mint','printing':'Surge Foil','language':'English','final_price':29.75,'qty':1,'sku':'123456789012'}
    path=Path(temp)/'sample.pdf';core.generate_zebra_pdf_labels([sample],str(path),layout='side',dpi=203)
    import fitz
    with fitz.open(path) as doc:
        doc[0].get_pixmap(matrix=fitz.Matrix(5,5)).save(str(ASSETS/'side-label.png'))
print('Captured manual assets:',ASSETS)
