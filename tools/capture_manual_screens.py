"""Capture only fictional training data for the simplified operator guide."""
from pathlib import Path
import csv,io,shutil,sys,tempfile,threading
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import roca_to_square as core
from station_service import StationService
from playwright.sync_api import sync_playwright,expect
ROOT=Path(__file__).resolve().parents[1];ASSETS=ROOT/'docs/manual-assets';ASSETS.mkdir(parents=True,exist_ok=True)
rows=[['Snapcaster Mage','0808','Near Mint','Foil','SLD','English','24.75','1','3'],['Sol Ring','0236','Lightly Played','Normal','CMM','English','3.75','1','4'],['Lightning Bolt','0141','Near Mint','Normal','M11','English','1.50','2','2'],['Example premium card','0012','Near Mint','Textured Foil','SET','English','45.00','1','1']]
f=io.StringIO();writer=csv.writer(f);writer.writerow(['Card Name','Card #','Condition','Printing','Set Code','Language','Price','Bin','Qty']);writer.writerows(rows);CSV=f.getvalue()
with tempfile.TemporaryDirectory() as temp,patch.object(core,'SETTINGS_FILE',str(Path(temp)/'settings.json')),patch.object(core,'OUTPUT_DIR',str(Path(temp)/'outputs')),patch.object(core,'upload_to_square',side_effect=AssertionError('No Square uploads in manual capture')):
    server=core.HTTPServer(('127.0.0.1',0),core.RocaStationHandler);service=StationService(core);server.station=service
    receipt=service.import_csv(CSV,'Sample shop cards.csv','labels')['receipt']
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        with sync_playwright() as p:
            browser=p.chromium.launch(executable_path=shutil.which('chromium'),headless=True,args=['--no-sandbox'])
            page=browser.new_page(base_url=f'http://127.0.0.1:{server.server_port}',viewport={'width':1440,'height':960},device_scale_factor=1.5)
            errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
            def open_batch(identifier):
                page.locator('#openHistory').click();page.locator(f'#historyList [data-open="{identifier}"]').click();expect(page.locator('#historyDialog')).not_to_be_visible()
            def shot(name,selector=None):
                page.locator('#toast').evaluate('(e)=>e.hidden=true');page.evaluate('window.scrollTo(0,0)')
                if selector:page.locator(selector).screenshot(path=str(ASSETS/name))
                else:page.screenshot(path=str(ASSETS/name))
            page.goto('/');page.wait_for_selector('#version:text("2.2.0")');shot('welcome.png')
            page.locator('#openAdmin').click();shot('admin-setup.png','#adminDialog')
            page.locator('#adminPassword').fill('Training-capture-only-246!');page.locator('#confirmPassword').fill('Training-capture-only-246!');page.locator('#authSubmit').click()
            expect(page.locator('#adminSettings')).to_be_visible();page.locator('#adminDialog').evaluate('(e)=>e.scrollTop=0');shot('admin-settings.png','#adminDialog')
            with page.expect_response(lambda r:r.url.endswith('/api/auth/logout') and r.status==200):page.locator('#closeAdmin').click()
            folder=Path(temp)/'Exports';folder.mkdir()
            service.save_preferences({'watchFolder':str(folder),'watchMode':'labels'});service.poll_exports()
            page.evaluate('()=>pollStation()')
            page.locator('#pickupFolderHint').evaluate('(e,text)=>e.textContent=text',r'Export folder: C:\Users\Store\Downloads')
            shot('export-pickup.png','#emptyState')
            open_batch(receipt['id']);shot('workspace.png');shot('card-table.png','.card-table-panel')
            page.locator('[data-edit]').first.click();page.locator('#editFinish').fill('Surge Foil');page.locator('#editPrice').fill('29.75');page.locator('#editReviewed').check();shot('card-editor.png','#cardDrawer');page.locator('#closeDrawer').click()
            page.locator('#markReviewed').click();page.locator('#confirmOkay').click();page.locator('#reviewContinue').click();expect(page.locator('#finishView')).to_be_visible();shot('finish.png','#finishView')
            page.locator('[data-intent="existing"]').click();page.locator('#primaryAction').click();expect(page.locator('#doneView')).to_be_visible();shot('done.png','#doneView')
            trade=service.import_csv(CSV,'Sample customer collection.csv','trade',new_delivery=True)['receipt']
            for card in trade['cards']:card['reviewed']=True
            trade=service.save_receipt(trade['id'],trade)
            page.locator('#openHistory').click();shot('history.png','#historyDialog');page.locator('#closeHistory').click()
            open_batch(trade['id']);page.locator('#reviewContinue').click();expect(page.locator('#tradePanel')).to_be_visible();shot('trade-offers.png','#tradePanel')
            online=service.import_csv(CSV,'Sample online stock.csv','online',new_delivery=True)['receipt'];online.update(storage_strategy='display',storage_location='Display case 2 · shelf B',inventory_intent='existing')
            for card in online['cards']:card['reviewed']=True
            online=service.save_receipt(online['id'],online)
            open_batch(online['id']);page.locator('#reviewContinue').click();expect(page.locator('#onlinePanel')).to_be_visible();shot('online-storage.png','#onlinePanel');shot('label-actions.png','.action-panel')
            service.create_task({'channel':'TCGplayer','order_reference':'SAMPLE-1001','card_name':'Snapcaster Mage · Surge Foil · Near Mint','qty':1,'storage_location':'Display case 2 · shelf B'})
            page.locator('[data-mode="activity"]').click();page.wait_for_selector('.task-card');shot('pull-task.png','#taskList')
            assert not errors,errors;browser.close()
    finally:server.shutdown();server.server_close();thread.join()
with tempfile.TemporaryDirectory() as temp:
    sample={'card_name':'Snapcaster Mage','card_num':'0808','card_set':'SLD','condition':'Near Mint','printing':'Surge Foil','language':'English','final_price':29.75,'qty':1,'sku':'123456789012'}
    path=Path(temp)/'sample.pdf';core.generate_zebra_pdf_labels([sample],str(path),layout='side',dpi=203)
    import fitz
    with fitz.open(path) as doc:doc[0].get_pixmap(matrix=fitz.Matrix(5,5)).save(str(ASSETS/'side-label.png'))
print('Captured manual assets:',ASSETS)
