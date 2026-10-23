from playwright.sync_api import sync_playwright,expect
import json
import os
import shutil
CSV=b'Card Name,Card #,Condition,Printing,Set Code,Language,Price,Bin,Qty\nSnapcaster Mage,0808,Near Mint,Foil,SLD,English,24.75,1,2\nSol Ring,0236,Lightly Played,Normal,CMM,English,3.75,2,4\n'
def run(BASE):
 with sync_playwright() as p:
  browser=p.chromium.launch(executable_path=os.environ.get('ROCA_BROWSER_PATH') or shutil.which('chromium'),headless=True,args=['--no-sandbox'])
  page=browser.new_page(base_url=BASE,viewport={'width':1440,'height':1050},device_scale_factor=1)
  errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
  page.goto(BASE);page.wait_for_selector('#version:text("2.0.3")')
  page.evaluate('window.scrollTo(0,0)');page.locator('#toast').evaluate('(e)=>e.hidden=true');page.screenshot(path=SCREENSHOTS+'/station2-empty.png',full_page=True)
  page.locator('[data-mode="trade"]').click()
  page.locator('#csvFile').set_input_files({'name':'Customer collection.csv','mimeType':'text/csv','buffer':CSV})
  expect(page.locator('#inventoryDialog')).to_be_visible()
  page.locator('[data-choose-intent="new"]').click()
  page.wait_for_selector('#receiptWorkspace:not([hidden])')
  expect(page.locator('#statCopies')).to_have_text('6')
  page.locator('[data-edit]').first.click()
  page.locator('#editFinish').fill('Galaxy Foil — Custom Treatment')
  page.locator('#editPrice').fill('28.50')
  page.locator('#editReviewed').check()
  page.locator('#cardForm button[type=submit]').click()
  expect(page.locator('#cardDrawer')).to_be_hidden()
  expect(page.locator('#cardRows')).to_contain_text('Galaxy Foil — Custom Treatment')
  page.locator('#markReviewed').click();page.locator('#confirmOkay').click()
  expect(page.locator('#statReviewed')).to_have_text('2 / 2')
  page.evaluate('saveReceipt()');page.wait_for_timeout(250);page.locator('#toast').evaluate('(e)=>e.hidden=true');page.evaluate('window.scrollTo(0,0)');page.screenshot(path=SCREENSHOTS+'/station2-trade.png',full_page=True)
  page.locator('#primaryAction').click();expect(page.locator('#confirmTitle')).to_have_text('Record the accepted buy?');page.locator('#confirmOkay').click()
  expect(page.locator('#receiptStatus')).to_have_text('BUY ACCEPTED')
  page.locator('#labelsOnly').click()
  expect(page.locator('#outputFiles')).to_be_visible()
  label_link=page.locator('#outputFiles a').filter(has_text='Open label PDF')
  assert page.request.get(label_link.get_attribute('href')).status==200
  page.locator('#moveReceipt').select_option('online')
  expect(page.locator('#pageTitle')).to_have_text('Online preparation')
  page.locator('#storageStrategy').select_option('display')
  page.locator('#storageLocation').fill('Display case 2 · shelf B');page.locator('#storageLocation').blur()
  page.locator('#prepareOnly').click()
  expect(page.locator('#receiptStatus')).to_have_text('LISTING PREPARED')
  listing=page.locator('#outputFiles a').filter(has_text='Download listing review CSV')
  response=page.request.get(listing.get_attribute('href'));assert response.status==200
  assert 'Galaxy Foil' in response.text() and 'Display case 2' in response.text()
  receipt_id=page.locator('#receiptReference').inner_text().split(' /')[0]
  data=page.request.get('/api/receipts/'+receipt_id).json()['receipt']
  assert data['inventory_status']=='not_received'
  page.evaluate('window.scrollTo(0,0)');page.locator('#toast').evaluate('(e)=>e.hidden=true');page.screenshot(path=SCREENSHOTS+'/station2-online.png',full_page=True)
  page.locator('[data-edit]').first.click();page.evaluate('window.scrollTo(0,0)');page.locator('#toast').evaluate('(e)=>e.hidden=true');page.screenshot(path=SCREENSHOTS+'/station2-drawer.png',full_page=True);page.locator('#closeDrawer').click()
  page.locator('#openAdmin').click()
  page.locator('#adminPassword').fill('Station-QA-password-246!');page.locator('#confirmPassword').fill('Station-QA-password-246!');page.locator('#authSubmit').click()
  expect(page.locator('#adminSettings')).to_be_visible()
  page.locator('#defaultCash').fill('40');page.locator('#adminForm button[type=submit]').click()
  expect(page.locator('#adminMessage')).to_contain_text('Settings saved')
  assert page.request.get('/api/settings').json()['cashPercent']==40
  assert 'squareToken' not in page.request.get('/api/settings').text()
  page.evaluate('window.scrollTo(0,0)');page.locator('#toast').evaluate('(e)=>e.hidden=true');page.locator('#adminDialog').evaluate('(e)=>e.scrollTop=0');page.screenshot(path=SCREENSHOTS+'/station2-admin.png')
  page.locator('#closeAdmin').click()
  page.wait_for_timeout(300)
  assert page.request.get('/api/admin/settings').status==403
  page.locator('#openAdmin').click();expect(page.locator('#adminAuth')).to_be_visible();page.evaluate('window.scrollTo(0,0)');page.locator('#toast').evaluate('(e)=>e.hidden=true');page.screenshot(path=SCREENSHOTS+'/station2-admin-locked.png');page.locator('#closeAdmin').click()
  page.locator('[data-mode="activity"]').click();page.locator('#newTask').click()
  page.locator('#taskOrder').fill('TCG-10001');page.locator('#taskCard').fill('Snapcaster Mage · Galaxy Foil · NM');page.locator('#taskLocation').fill('Display case 2 · shelf B');page.locator('#taskForm button[type=submit]').click()
  expect(page.locator('#taskList')).to_contain_text('TCG-10001')
  page.locator('[data-complete]').click();page.locator('#confirmOkay').click()
  expect(page.locator('#taskList')).to_contain_text('PULLED')
  page.evaluate('window.scrollTo(0,0)');page.locator('#toast').evaluate('(e)=>e.hidden=true');page.screenshot(path=SCREENSHOTS+'/station2-activity.png',full_page=True)
  page.locator('[data-mode="online"]').click();page.set_viewport_size({'width':390,'height':844});page.evaluate('window.scrollTo(0,0)');page.locator('#toast').evaluate('(e)=>e.hidden=true');page.screenshot(path=SCREENSHOTS+'/station2-mobile.png',full_page=True)
  assert not page.evaluate('document.documentElement.scrollWidth>innerWidth')
  page.set_viewport_size({'width':1440,'height':1050})
  # New batches, tabs and watched-folder notifications must leave a fresh import screen.
  page.locator('[data-mode="labels"]').click()
  expect(page.locator('#emptyState')).to_be_visible()
  second_csv=CSV.replace(b'Snapcaster Mage',b'Example second batch')
  page.locator('#csvFile').set_input_files({'name':'Second batch.csv','mimeType':'text/csv','buffer':second_csv})
  expect(page.locator('#inventoryDialog')).to_be_visible()
  page.locator('[data-choose-intent="existing"]').click()
  expect(page.locator('[data-intent="existing"]')).to_have_class('selected')
  expect(page.locator('[data-intent="new"]')).to_be_enabled()
  second_id=page.locator('#receiptReference').inner_text().split(' /')[0]
  page.locator('#receiptNotes').fill('Keep this saved when starting a new batch.')
  page.locator('#cardSearch').fill('a search to reset')
  page.locator('#newReceipt').click()
  expect(page.locator('#emptyState')).to_be_visible()
  expect(page.locator('#cardSearch')).to_have_value('')
  expect(page.locator('#receiptPicker')).to_have_value('')
  saved=page.request.get('/api/receipts/'+second_id).json()['receipt']
  assert saved['notes']=='Keep this saved when starting a new batch.'
  assert saved['inventory_intent']=='existing'
  third_csv=CSV.replace(b'Snapcaster Mage',b'Example third batch')
  page.locator('#csvFile').set_input_files({'name':'Third batch.csv','mimeType':'text/csv','buffer':third_csv})
  expect(page.locator('#inventoryDialog')).to_be_visible()
  page.locator('#inventoryLater').click()
  expect(page.locator('#primaryAction')).to_be_disabled()
  third_id=page.locator('#receiptReference').inner_text().split(' /')[0]
  assert page.request.get('/api/receipts/'+third_id).json()['receipt']['inventory_intent'] is None
  page.locator('[data-intent="new"]').click()
  page.locator('#markReviewed').click();page.locator('#confirmOkay').click()
  page.locator('#saveReceipt').click()
  expect(page.locator('#saveState')).to_have_text('All changes saved')
  assert page.request.get('/api/receipts/'+third_id).json()['receipt']['inventory_intent']=='new'
  page.locator('#deleteReceipt').click();page.locator('#confirmCancel').click()
  expect(page.locator('#receiptWorkspace')).to_be_visible()
  page.locator('#deleteReceipt').click();page.locator('#confirmOkay').click()
  expect(page.locator('#emptyState')).to_be_visible()
  expect(page.locator('#newReceipt')).to_be_enabled()
  assert third_id not in [r['id'] for r in page.request.get('/api/receipts').json()['receipts']]
  page.locator('#csvFile').set_input_files({'name':'Renamed third batch.csv','mimeType':'text/csv','buffer':third_csv})
  expect(page.locator('#confirmTitle')).to_have_text('Restore this deleted receipt?')
  page.locator('#confirmOkay').click()
  expect(page.locator('#receiptReference')).to_contain_text(third_id)
  expect(page.locator('[data-intent="new"]')).to_have_class('selected')
  expect(page.locator('[data-intent="new"]')).to_be_enabled()
  page.locator('[data-mode="labels"]').click()
  expect(page.locator('#emptyState')).to_be_visible()
  page.request.post('/api/import',data={'csv':CSV.decode().replace('Snapcaster Mage','Folder pickup example'),'fileName':'Folder draft.csv','mode':'labels'})
  page.wait_for_timeout(6500)
  expect(page.locator('#emptyState')).to_be_visible()
  page.locator('#receiptPicker').select_option(second_id)
  expect(page.locator('#receiptReference')).to_contain_text(second_id)
  expect(page.locator('[data-intent="existing"]')).to_have_class('selected')
  page.locator('[data-mode="bulk"]').click()
  large='Card Name,Card #,Condition,Printing,Price,Bin,Qty\n'+''.join(f'Inventory card {i},{i},Near Mint,Normal,1.00,1,1\n' for i in range(11000))
  page.locator('#csvFile').set_input_files({'name':'11000-card-export.csv','mimeType':'text/csv','buffer':large.encode()})
  expect(page.locator('#statCopies')).to_have_text('11,000',timeout=30000)
  expect(page.locator('#cardRows tr')).to_have_count(40)
  page.locator('#cardSearch').fill('Inventory card 10999')
  expect(page.locator('#cardRows tr')).to_have_count(1)
  assert not errors,errors
  print('PASS: trade/edit/review/buy/labels/transfer/online, downloads, admin, pull task, mobile, fresh batch and tab resets, per-batch inventory choices, save before reset, delete/cancel/restore, saved receipt reopening, folder notifications without auto-opening, 11,000-row pagination/search, no JS errors.')
  browser.close()

if __name__=='__main__':
 import sys,tempfile,threading
 from pathlib import Path
 from unittest.mock import patch
 import roca_to_square as app
 from station_service import StationService
 SCREENSHOTS=tempfile.mkdtemp(prefix='roca-ui-screenshots-')
 with tempfile.TemporaryDirectory() as directory,patch.object(app,'SETTINGS_FILE',str(Path(directory)/'settings.json')),patch.object(app,'OUTPUT_DIR',str(Path(directory)/'outputs')):
  server=app.HTTPServer(('127.0.0.1',0),app.RocaStationHandler)
  server.station=StationService(app)
  thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
  try:run(f'http://127.0.0.1:{server.server_port}')
  finally:server.shutdown();server.server_close();thread.join()
 print('Screenshots:',SCREENSHOTS)
