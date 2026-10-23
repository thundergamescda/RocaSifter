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
  page.goto(BASE);page.wait_for_selector('#version:text("2.0.1")')
  page.evaluate('window.scrollTo(0,0)');page.locator('#toast').evaluate('(e)=>e.hidden=true');page.screenshot(path=SCREENSHOTS+'/station2-empty.png',full_page=True)
  page.locator('[data-mode="trade"]').click()
  page.locator('#csvFile').set_input_files({'name':'Customer collection.csv','mimeType':'text/csv','buffer':CSV})
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
  page.locator('[data-mode="bulk"]').click()
  large='Card Name,Card #,Condition,Printing,Price,Bin,Qty\n'+''.join(f'Inventory card {i},{i},Near Mint,Normal,1.00,1,1\n' for i in range(11000))
  page.locator('#csvFile').set_input_files({'name':'11000-card-export.csv','mimeType':'text/csv','buffer':large.encode()})
  expect(page.locator('#statCopies')).to_have_text('11,000',timeout=30000)
  expect(page.locator('#cardRows tr')).to_have_count(40)
  page.locator('#cardSearch').fill('Inventory card 10999')
  expect(page.locator('#cardRows tr')).to_have_count(1)
  assert not errors,errors
  print('PASS: trade import, arbitrary finish edit, review, accepted buy, labels without stock, transfer, display-case preparation, CSV/PDF downloads, admin setup/save/lock, pull task, mobile overflow, 11,000-row import/pagination/search, no JS errors.')
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
