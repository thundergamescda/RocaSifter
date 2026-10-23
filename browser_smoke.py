"""Exercise the operator workflow in an isolated store with mocked Square writes."""
from pathlib import Path
import copy
import json
import os
import shutil
import tempfile
import threading
from unittest.mock import patch
from playwright.sync_api import sync_playwright, expect

CSV=b'Card Name,Card #,Condition,Printing,Set Code,Language,Price,Bin,Qty\nSnapcaster Mage,0808,Near Mint,Foil,SLD,English,24.75,1,2\nSol Ring,0236,Lightly Played,Normal,CMM,English,3.75,2,4\n'


def run(base, screenshots, square_calls):
    with sync_playwright() as p:
        browser=p.chromium.launch(executable_path=os.environ.get('ROCA_BROWSER_PATH') or shutil.which('chromium'),headless=True,args=['--no-sandbox'])
        page=browser.new_page(base_url=base,viewport={'width':1440,'height':1000})
        errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
        def shot(name):
            page.locator('#toast').evaluate('(e)=>e.hidden=true');page.evaluate('window.scrollTo(0,0)')
            page.screenshot(path=str(Path(screenshots)/name),full_page=True)
        def import_cards(name,content):
            page.locator('#csvFile').set_input_files({'name':name,'mimeType':'text/csv','buffer':content})
            expect(page.locator('#reviewView')).to_be_visible()
            expect(page.locator('#newReceipt')).to_be_enabled()
            assert page.locator('#inventoryDialog').count()==0
            assert page.request.get('/api/receipts/'+batch_id()).json()['receipt']['inventory_intent'] is None
        def batch_id():return page.locator('#receiptReference').inner_text().rsplit(' · ',1)[1]
        def check_all():
            page.locator('#markReviewed').click();page.locator('#confirmOkay').click()
            expect(page.locator('#reviewContinue')).to_be_enabled()
        def finish():
            page.locator('#reviewContinue').click();expect(page.locator('#finishView')).to_be_visible()
        def history(receipt_id):
            page.locator('#openHistory').click()
            page.locator(f'#historyList [data-open="{receipt_id}"]').click()
            expect(page.locator('#historyDialog')).not_to_be_visible()
            expect(page.locator('#receiptReference')).to_contain_text(receipt_id)
        def new_batch():
            if page.locator('#doneView').is_visible():page.locator('#nextBatch').click()
            else:page.locator('#newReceipt').click()
            expect(page.locator('#emptyState')).to_be_visible()
            expect(page.locator('#cardSearch')).to_have_value('')
        page.goto('/');page.wait_for_selector('#version:text("2.1.0")')
        expect(page.locator('#pageTitle')).to_have_text('Process cards')
        assert not page.locator('#moreToolsNav').evaluate('(e)=>e.open')
        shot('station-simple-home.png')
        import_cards('Shop cards.csv',CSV)
        expect(page.locator('#statCopies')).to_have_text('6')
        assert page.locator('.card-table-panel').bounding_box()['y']<520,'Cards should appear before the fold'
        expect(page.locator('#finishView')).not_to_be_visible()
        page.locator('[data-edit]').first.click()
        page.locator('#editFinish').fill('Galaxy Foil — Custom Treatment');page.locator('#editPrice').fill('28.50')
        page.locator('#editReviewed').check();page.locator('#cardForm button[type=submit]').click()
        expect(page.locator('#cardDrawer')).not_to_be_visible()
        expect(page.locator('#cardRows')).to_contain_text('Galaxy Foil — Custom Treatment')
        page.locator('[data-edit]').first.click();page.locator('#splitCopy').click()
        expect(page.locator('#editQty')).to_have_value('1')
        page.locator('#editFinish').fill('Future Custom Foil');page.locator('#editPrice').fill('32.00')
        page.locator('#editReviewed').check();page.locator('#cardForm button[type=submit]').click()
        expect(page.locator('#cardRows tr')).to_have_count(3)
        expect(page.locator('#cardRows')).to_contain_text('Future Custom Foil')
        expect(page.locator('#statCopies')).to_have_text('6')
        page.locator('[data-review]').last.check()
        check_all();first_id=batch_id();shot('station-simple-review.png');finish()
        expect(page.locator('#primaryAction')).to_be_disabled()
        expect(page.locator('#printOptions')).not_to_have_attribute('open','')
        shot('station-simple-finish.png')
        page.locator('[data-intent="existing"]').click();page.locator('#primaryAction').click()
        expect(page.locator('#doneView')).to_be_visible();expect(page.locator('#doneSummary')).to_contain_text('No inventory')
        assert not square_calls
        pdf=page.locator('#printLabels').get_attribute('href')
        assert page.request.get(pdf).status==200
        first=page.request.get('/api/receipts/'+first_id).json()['receipt']
        assert first['inventory_status']=='not_received' and first['inventory_intent']=='existing'
        shot('station-simple-done.png')
        new_batch()
        second_csv=CSV.replace(b'Snapcaster Mage',b'Second incoming stack')
        import_cards('New incoming cards.csv',second_csv);second_id=batch_id();check_all();finish()
        expect(page.locator('#primaryAction')).to_be_disabled()
        page.locator('[data-intent="new"]').click();page.locator('#primaryAction').click()
        expect(page.locator('#confirmTitle')).to_have_text('Add these copies to Square?')
        page.locator('#confirmCancel').click();assert not square_calls
        page.locator('#primaryAction').click();page.locator('#confirmOkay').click()
        expect(page.locator('#doneView')).to_be_visible();expect(page.locator('#doneSummary')).to_contain_text('confirmed in Square')
        assert len(square_calls)==1 and sum(c['qty'] for c in square_calls[0])==6
        new_batch();history(second_id)
        expect(page.locator('#doneView')).to_be_visible()
        page.locator('#doneReview').click();expect(page.locator('[data-review]').first).to_be_disabled()
        finish();expect(page.locator('#inventoryIntent')).not_to_be_visible()
        page.locator('#primaryAction').click();expect(page.locator('#doneView')).to_be_visible()
        assert len(square_calls)==1,'Reprinting must never receive stock again'
        page.locator('#batchOptions').evaluate('(e)=>e.open=true')
        page.locator('#deleteReceipt').click();page.locator('#confirmCancel').click()
        expect(page.locator('#doneView')).to_be_visible()
        page.locator('#deleteReceipt').click();page.locator('#confirmOkay').click()
        expect(page.locator('#emptyState')).to_be_visible()
        assert second_id not in [r['id'] for r in page.request.get('/api/receipts').json()['receipts']]
        page.locator('#csvFile').set_input_files({'name':'Same received cards.csv','mimeType':'text/csv','buffer':second_csv})
        expect(page.locator('#confirmTitle')).to_have_text('Restore this deleted batch?')
        page.locator('#confirmOkay').click();expect(page.locator('#doneView')).to_be_visible()
        assert batch_id()==second_id and len(square_calls)==1
        # Saved work and filters survive/reset in the appropriate places.
        new_batch();history(first_id);page.locator('#doneReview').click()
        page.locator('#batchOptions').evaluate('(e)=>e.open=true')
        page.locator('#receiptNotes').fill('Saved before starting the next batch.')
        page.locator('#cardSearch').fill('A search to reset');page.locator('#newReceipt').click()
        expect(page.locator('#emptyState')).to_be_visible()
        assert page.request.get('/api/receipts/'+first_id).json()['receipt']['notes']=='Saved before starting the next batch.'
        # Printer choices belong to Admin; saved defaults reach the next batch.
        page.locator('#openAdmin').click();page.locator('#adminPassword').fill('Station-QA-password-246!')
        page.locator('#confirmPassword').fill('Station-QA-password-246!');page.locator('#authSubmit').click()
        expect(page.locator('#adminSettings')).to_be_visible()
        page.locator('#defaultDpi').select_option('300');page.locator('#defaultLayout').select_option('standard')
        page.locator('#adminForm button[type=submit]').click();expect(page.locator('#adminMessage')).to_contain_text('Settings saved')
        with page.expect_response(lambda response:response.url.endswith('/api/auth/logout') and response.status==200):
            page.locator('#closeAdmin').click()
        expect(page.locator('#adminDialog')).not_to_be_visible()
        assert page.request.get('/api/admin/settings').status==403
        page.locator('#openAdmin').click();expect(page.locator('#adminAuth')).to_be_visible();page.locator('#closeAdmin').click()
        # Trade-ins show the offer only after card review, before labels or stock.
        page.locator('[data-mode="trade"]').click()
        trade_csv=CSV.replace(b'Snapcaster Mage',b'Customer collection card')
        import_cards('Customer collection.csv',trade_csv);trade_id=batch_id()
        expect(page.locator('#tradePanel')).not_to_be_visible();check_all();finish()
        expect(page.locator('#tradePanel')).to_be_visible();expect(page.locator('#labelsPanel')).not_to_be_visible()
        page.locator('#creditOffer').click();page.locator('#primaryAction').click()
        expect(page.locator('#confirmTitle')).to_have_text('Record the agreed purchase?')
        page.locator('#confirmOkay').click();expect(page.locator('#acceptedBuy')).to_be_visible()
        expect(page.locator('#labelsPanel')).to_be_visible();expect(page.locator('#primaryAction')).to_be_disabled()
        expect(page.locator('#labelLayout')).to_have_value('standard');expect(page.locator('#printerDpi')).to_have_value('300')
        page.locator('[data-intent="existing"]').click();page.locator('#primaryAction').click()
        expect(page.locator('#doneView')).to_be_visible();assert len(square_calls)==1
        trade=page.request.get('/api/receipts/'+trade_id).json()['receipt'];assert trade['purchase']['type']=='credit'
        assert page.request.get('/files/'+trade['files']['buy']).status==200
        # Transfer the same batch into the advanced online workflow. Missing storage blocks stock.
        page.locator('#batchOptions').evaluate('(e)=>e.open=true');page.locator('#moveReceipt').select_option('online')
        expect(page.locator('#pageTitle')).to_have_text('Online preparation');finish()
        page.locator('[data-intent="new"]').click();page.locator('#primaryAction').click()
        expect(page.locator('#toast')).to_contain_text('storage location')
        assert len(square_calls)==1
        page.locator('#storageStrategy').select_option('display');page.locator('#storageLocation').fill('Display case 2 · shelf B')
        page.locator('#storageLocation').blur();page.locator('[data-intent="existing"]').click();page.locator('#primaryAction').click()
        expect(page.locator('#doneView')).to_be_visible();assert len(square_calls)==1
        listing=page.request.get(page.locator('#printLabels').get_attribute('href'))
        assert listing.status==200 and 'Display case 2' in listing.text()
        # Advanced tools stay available, including manual order pulls.
        page.locator('[data-mode="activity"]').click();page.locator('#newTask').click()
        page.locator('#taskOrder').fill('TCG-10001');page.locator('#taskCard').fill('Customer collection card')
        page.locator('#taskLocation').fill('Display case 2 · shelf B');page.locator('#taskForm button[type=submit]').click()
        page.locator('[data-complete]').click();page.locator('#confirmOkay').click()
        expect(page.locator('#taskList')).to_contain_text('PULLED');assert len(square_calls)==1
        # Folder arrivals notify without replacing a fresh import screen.
        page.locator('[data-mode="labels"]').click();expect(page.locator('#emptyState')).to_be_visible()
        page.request.post('/api/import',data={'csv':CSV.decode().replace('Snapcaster Mage','Folder pickup card'),'fileName':'Folder draft.csv','mode':'labels'})
        page.wait_for_timeout(6500);expect(page.locator('#emptyState')).to_be_visible()
        # Check the actual card and finish views on a small screen.
        history(first_id);page.locator('#doneReview').click();page.set_viewport_size({'width':390,'height':844})
        assert not page.evaluate('document.documentElement.scrollWidth>innerWidth')
        shot('station-simple-mobile.png');finish()
        assert not page.evaluate('document.documentElement.scrollWidth>innerWidth')
        page.set_viewport_size({'width':1440,'height':1000})
        page.locator('#moreToolsNav > summary').click()
        page.locator('[data-mode="bulk"]').click()
        large='Card Name,Card #,Condition,Printing,Price,Bin,Qty\n'+''.join(f'Inventory card {i},{i},Near Mint,Normal,1.00,1,1\n' for i in range(11000))
        import_cards('11000-card-export.csv',large.encode());large_id=batch_id()
        expect(page.locator('#statCopies')).to_have_text('11,000',timeout=30000);expect(page.locator('#cardRows tr')).to_have_count(40)
        page.locator('#cardSearch').fill('Inventory card 10999');expect(page.locator('#cardRows tr')).to_have_count(1)
        page.locator('#cardSearch').fill('');page.locator('#minPrice').fill('1.25');page.locator('#applyThreshold').click()
        expect(page.locator('#statCopies')).to_have_text('0');expect(page.locator('#reviewContinue')).to_be_disabled()
        page.locator('#minPrice').fill('1');page.locator('#applyThreshold').click()
        page.locator('#reviewContinue').click();expect(page.locator('#pageTitle')).to_have_text('Process cards')
        assert batch_id()==large_id and len(square_calls)==1
        expect(page.locator('#reviewView')).to_be_visible();expect(page.locator('#reviewContinue')).to_be_disabled()
        assert not errors,errors
        print('PASS: guided import/check/finish, cards above fold, labels without stock, explicit Square confirmation/cancel, one stock addition, safe reprints/delete/restore, fresh batches and saved edits, arbitrary finishes/split copies, Admin defaults/lock, trade approval, online storage guard and files, pull tasks, folder notifications, mobile, 11,000-row filters/paging/search; no JS errors or live Square requests.')
        browser.close()


if __name__=='__main__':
    import roca_to_square as app
    from station_service import StationService
    screenshots=tempfile.mkdtemp(prefix='roca-ui-screenshots-');square_calls=[]
    def upload(cards,token,**kwargs):
        assert token=='fake-sandbox-token-for-browser-test'
        square_calls.append(copy.deepcopy(cards))
        return {'items_uploaded':len(cards),'inventory_added':sum(c['qty'] for c in cards),'errors':[]}
    with tempfile.TemporaryDirectory() as directory,patch.object(app,'SETTINGS_FILE',str(Path(directory)/'settings.json')),patch.object(app,'OUTPUT_DIR',str(Path(directory)/'outputs')),patch.object(app,'upload_to_square',side_effect=upload),patch.object(app.requests,'get',side_effect=AssertionError('Live Square requests are disabled')),patch.object(app.requests,'post',side_effect=AssertionError('Live Square requests are disabled')):
        server=app.HTTPServer(('127.0.0.1',0),app.RocaStationHandler);server.station=StationService(app)
        service=server.station;service.vault.set('fake-sandbox-token-for-browser-test')
        pref=service.preferences();pref.update(squareEnvironment='sandbox',squareLocationId='test-store');service.store.put('preferences',pref)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:run(f'http://127.0.0.1:{server.server_port}',screenshots,square_calls)
        finally:server.shutdown();server.server_close();thread.join()
    print('Screenshots:',screenshots)
