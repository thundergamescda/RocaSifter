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


def run(base, screenshots, square_calls, export_folder):
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
            expect(page.locator('#finishStepText')).to_have_text('Finish')
        page.goto('/');page.wait_for_selector('#version:text("2.3.1")')
        expect(page.locator('#pageTitle')).to_have_text('Process cards')
        assert not page.locator('#moreToolsNav').evaluate('(e)=>e.open')
        shot('station-simple-home.png')
        import_cards('Shop cards.csv',CSV)
        expect(page.locator('#statCopies')).to_have_text('6')
        assert page.locator('.card-table-panel').bounding_box()['y']<520,'Cards should appear before the fold'
        expect(page.locator('#finishView')).not_to_be_visible()
        page.locator('[data-edit]').first.click()
        page.locator('#editFinish').fill('Galaxy Foil — Custom Treatment');page.locator('#editPrice').fill('28.50')
        page.locator('#compareCardPrices').click();expect(page.locator('#priceResearchDialog')).to_be_visible()
        page.locator('#priceResearchGame').select_option('magic')
        expect(page.locator('#priceResearchLinks a').last).to_have_attribute('href',__import__('re').compile('/search/magic/product'))
        page.keyboard.press('Escape');expect(page.locator('#priceResearchDialog')).not_to_be_visible()
        expect(page.locator('#cardDrawer')).to_be_visible();expect(page.locator('#editPrice')).to_have_value('28.50')
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
        page.locator('#useDownloads').click();expect(page.locator('#watchFolder')).to_have_value(str(export_folder))
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
        # Real browser download pickup: no import endpoint or file chooser is used.
        page.locator('[data-mode="labels"]').click();expect(page.locator('#emptyState')).to_be_visible()
        expect(page.locator('#emptyTitle')).to_have_text('Ready for your next export.')
        expect(page.locator('#pickupFolderHint')).to_contain_text(str(export_folder))
        shot('station-export-waiting.png')
        export_a=CSV.replace(b'Snapcaster Mage',b'Automatic export card A')
        partial=export_folder/'Roca A.csv.crdownload';partial.write_bytes(export_a)
        page.wait_for_timeout(5000);expect(page.locator('#emptyState')).to_be_visible()
        partial.rename(export_folder/'Roca A.csv')
        expect(page.locator('#cardRows')).to_contain_text('Automatic export card A',timeout=15000)
        pickup_a=batch_id()
        expect(page.locator('#reviewContinue')).to_be_disabled()
        receipt=page.request.get('/api/receipts/'+pickup_a).json()['receipt']
        assert receipt['inventory_intent'] is None and receipt['files']=={} and not receipt['pickup_pending']
        assert len(square_calls)==1
        # More exports queue while a card editor or batch is in use.
        page.locator('[data-edit]').first.click()
        page.locator('#editFinish').fill('Unfinished handwritten treatment')
        (export_folder/'Roca B.CSV').write_bytes(CSV.replace(b'Snapcaster Mage',b'Automatic export card B'))
        expect(page.locator('#pickupNoticeText')).to_have_text('1 export is ready to review.',timeout=15000)
        assert batch_id()==pickup_a
        expect(page.locator('#cardDrawer')).to_be_visible()
        expect(page.locator('#editFinish')).to_have_value('Unfinished handwritten treatment')
        page.locator('#cardForm button[type=submit]').click();expect(page.locator('#cardDrawer')).not_to_be_visible()
        page.locator('#newReceipt').click()
        expect(page.locator('#cardRows')).to_contain_text('Automatic export card B')
        pickup_b=batch_id();assert pickup_b!=pickup_a and len(square_calls)==1
        assert page.request.get('/api/receipts/'+pickup_a).json()['receipt']['cards'][0]['printing']=='Unfinished handwritten treatment'
        check_all();finish();page.locator('[data-intent="existing"]').click();page.locator('#primaryAction').click()
        expect(page.locator('#doneView')).to_be_visible();new_batch()
        # Renamed, repeated exports never recreate or reopen a processed batch.
        (export_folder/'Roca B (1).csv').write_bytes(CSV.replace(b'Snapcaster Mage',b'Automatic export card B'))
        page.wait_for_timeout(7500);expect(page.locator('#emptyState')).to_be_visible()
        assert page.request.get('/api/receipts').json()['pickupQueue']==[] and len(square_calls)==1
        # A queued export survives a closed browser and reopens for review on startup.
        page.locator('#moreToolsNav > summary').click()
        page.locator('[data-mode="activity"]').click()
        (export_folder/'Roca C.csv').write_bytes(CSV.replace(b'Snapcaster Mage',b'Automatic export card C'))
        page.wait_for_timeout(7500)
        assert len(page.request.get('/api/receipts').json()['pickupQueue'])==1
        page.reload()
        expect(page.locator('#cardRows')).to_contain_text('Automatic export card C',timeout=15000)
        assert len(square_calls)==1 and page.request.get('/api/receipts').json()['pickupQueue']==[]
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
        # Manual-only labels: cancellation and a lost response must never duplicate a card.
        new_batch();before_manual=len(page.request.get('/api/receipts').json()['receipts'])
        page.locator('#addManualItem').click();page.locator('#manualName').fill('Cancelled entry')
        page.locator('#cancelManualItem').click();expect(page.locator('#emptyState')).to_be_visible()
        assert len(page.request.get('/api/receipts').json()['receipts'])==before_manual
        page.locator('#addManualItem').click();page.locator('#manualName').fill('Charizard · PSA 10')
        page.locator('#manualPrice').fill('123.47');page.locator('#manualCondition').fill('PSA 10')
        page.locator('#compareManualPrices').click();page.locator('#priceResearchGame').select_option('pokemon')
        expect(page.locator('#priceResearchLinks a')).to_have_count(4)
        expect(page.locator('#priceResearchHint')).to_contain_text('same grader')
        expect(page.locator('#priceResearchLinks a').first).to_have_attribute('href',__import__('re').compile('LH_Sold=1'))
        expect(page.locator('#priceResearchLinks a').last).to_contain_text('raw reference')
        shot('station-direct-price-comparison.png')
        page.set_viewport_size({'width':390,'height':844})
        assert not page.evaluate('document.documentElement.scrollWidth>innerWidth')
        shot('station-direct-price-comparison-mobile.png');page.set_viewport_size({'width':1440,'height':1000})
        page.locator('#returnToItem').click();expect(page.locator('#manualItemDialog')).to_be_visible()
        expect(page.locator('#manualPrice')).to_have_value('123.47')
        shot('station-manual-item.png')
        page.set_viewport_size({'width':390,'height':844})
        assert not page.evaluate('document.documentElement.scrollWidth>innerWidth')
        shot('station-manual-item-mobile.png');page.set_viewport_size({'width':1440,'height':1000})
        first_response=[True]
        def lose_once(route):
            response=route.fetch()
            if first_response[0]:first_response[0]=False;route.abort('failed')
            else:route.fulfill(response=response)
        page.route('**/api/manual-items',lose_once)
        page.locator('#saveManualItem').click()
        expect(page.locator('#manualItemMessage')).to_contain_text('retry safely')
        assert len(page.request.get('/api/receipts').json()['receipts'])==before_manual+1
        page.locator('#saveManualItem').click();expect(page.locator('#manualItemDialog')).not_to_be_visible()
        page.unroute('**/api/manual-items',lose_once)
        manual_id=batch_id();expect(page.locator('#cardRows tr')).to_have_count(1)
        expect(page.locator('#cardRows')).to_contain_text('$123.47');expect(page.locator('#reviewContinue')).to_be_disabled()
        assert page.request.get('/api/receipts/'+manual_id).json()['receipt']['cards'][0]['qty']==1
        check_all();finish();page.locator('[data-intent="existing"]').click();page.locator('#primaryAction').click()
        expect(page.locator('#doneView')).to_be_visible();assert len(square_calls)==1
        assert page.request.get(page.locator('#printLabels').get_attribute('href')).status==200
        # Mixed trade-in: a custom selling price participates in the unchanged buying percentages.
        page.locator('[data-mode="trade"]').click()
        import_cards('Mixed manual trade.csv',CSV.replace(b'Snapcaster Mage',b'Mixed trade card'))
        mixed_id=batch_id();original=page.request.get('/api/receipts/'+mixed_id).json()['receipt']['cards']
        page.locator('#addManualItem').click();expect(page.locator('#manualItemDescription')).to_contain_text('normal cash')
        page.locator('#manualName').fill('PSA slab added to trade');page.locator('#manualPrice').fill('100.13')
        page.locator('#manualQty').fill('2');page.locator('#manualCondition').fill('PSA 9')
        page.locator('#saveManualItem').click();expect(page.locator('#manualItemDialog')).not_to_be_visible()
        assert batch_id()==mixed_id
        saved=page.request.get('/api/receipts/'+mixed_id).json()['receipt']
        assert saved['cards'][:-1]==original and saved['cards'][-1]['final_price']==100.13
        expect(page.locator('#statCopies')).to_have_text('8')
        check_all();finish();expect(page.locator('#cashAmount')).to_have_text('$92.67')
        expect(page.locator('#creditAmount')).to_have_text('$132.38')
        page.locator('#primaryAction').click();page.locator('#confirmOkay').click()
        expect(page.locator('#acceptedBuy')).to_contain_text('$92.67')
        expect(page.locator('#addManualItem')).to_be_disabled()
        accepted=page.request.get('/api/receipts/'+mixed_id).json()['receipt']
        assert accepted['purchase']['amount']==92.67 and accepted['cards'][-1]['final_price']==100.13
        page.locator('[data-intent="existing"]').click();page.locator('#primaryAction').click()
        expect(page.locator('#doneView')).to_be_visible();assert len(square_calls)==1
        # Manual inventory uses the same explicit receiving action and reprint protections.
        page.locator('[data-mode="labels"]').click();page.locator('#addManualItem').click()
        page.locator('#manualName').fill('Inventory slab');page.locator('#manualCondition').fill('PSA 9')
        page.locator('#manualPrice').fill('45.19');page.locator('#manualQty').fill('2')
        page.locator('#saveManualItem').click();expect(page.locator('#manualItemDialog')).not_to_be_visible()
        manual_stock=batch_id();check_all();finish();page.locator('[data-intent="new"]').click()
        page.locator('#primaryAction').click();expect(page.locator('#confirmText')).to_contain_text('2 copies')
        page.locator('#confirmOkay').click();expect(page.locator('#doneView')).to_be_visible()
        assert len(square_calls)==2 and square_calls[-1][0]['final_price']==45.19
        assert square_calls[-1][0]['condition']=='PSA 9' and square_calls[-1][0]['qty']==2
        expect(page.locator('#addManualItem')).to_be_disabled()
        new_batch();history(manual_stock);page.locator('#doneReview').click();finish()
        page.locator('#primaryAction').click();expect(page.locator('#doneView')).to_be_visible()
        assert len(square_calls)==2
        assert not errors,errors
        print('PASS: guided import/check/finish, cards above fold, labels without stock, explicit Square confirmation/cancel, one stock addition, safe reprints/delete/restore, fresh batches and saved edits, arbitrary finishes/split copies, Admin defaults/lock, trade approval, online storage guard and files, pull tasks, real export pickup, partial downloads, queued drafts, restart/reopen, duplicate reexports, mobile, 11,000-row filters/paging/search, manual-only labels, graded identity, exact custom prices, lost-response retry, mixed trade percentages and manual receiving/reprints; no JS errors or live Square requests.')
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
        export_folder=Path(directory)/'Downloads';export_folder.mkdir()
        (export_folder/'old.csv').write_bytes(CSV.replace(b'Snapcaster Mage',b'Old download must stay ignored'))
        service.start_watcher()
        try:
            with patch.object(service,'downloads_folder',return_value=str(export_folder)):
                run(f'http://127.0.0.1:{server.server_port}',screenshots,square_calls,export_folder)
        finally:
            service.stop_event.set();service.watcher.join(timeout=3)
            server.shutdown();server.server_close();thread.join()
    print('Screenshots:',screenshots)
